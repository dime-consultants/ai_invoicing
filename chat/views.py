# chat/views.py
import logging

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import generics, permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import ChatConversation, ChatMessage, ChatMessageAttachment, Workflow
from .serializers import (
    ChatConversationSerializer,
    ChatConversationListSerializer,
    ChatMessageSerializer,
    ChatMessageAttachmentSerializer,
    WorkflowSerializer,
)
from .services import ChatService

logger = logging.getLogger(__name__)


class ChatInterfaceView(TemplateView):
    """Serve the invoice processing chat UI."""
    template_name = "chat/interface.html"


# ─────────────────────────────────────────────────────────────────────────────
# Conversations
# ─────────────────────────────────────────────────────────────────────────────

class ChatConversationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/chat/conversations/   — list user's conversations (no messages)
    POST /api/chat/conversations/   — create a new conversation
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        return ChatConversationListSerializer if self.request.method == "GET" else ChatConversationSerializer

    def get_queryset(self):
        return ChatConversation.objects.filter(
            user=self.request.user
        ).order_by("-updated_at")

    def create(self, request, *args, **kwargs):
        title = (request.data.get("title") or "Untitled Conversation").strip()
        conv = ChatConversation.objects.create(user=request.user, title=title)
        return Response(ChatConversationSerializer(conv).data, status=status.HTTP_201_CREATED)


class ChatConversationDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/chat/conversations/<id>/
    PATCH  /api/chat/conversations/<id>/   — title / is_active only
    DELETE /api/chat/conversations/<id>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChatConversationSerializer

    def get_queryset(self):
        return ChatConversation.objects.filter(user=self.request.user)

    def update(self, request, *args, **kwargs):
        allowed = {"title", "is_active"}
        data = {k: v for k, v in request.data.items() if k in allowed}
        instance = self.get_object()
        for field, value in data.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(data.keys()) + ["updated_at"])
        return Response(ChatConversationSerializer(instance).data)


# ─────────────────────────────────────────────────────────────────────────────
# Send message
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessageSendView(APIView):
    """
    POST /api/chat/conversations/<conversation_id>/send/

    Accepts multipart/form-data:
        message         str       required
        files           file[]    optional  — any number of attachments
        workflow_id     int|string optional

    Returns:
        user_message        saved user ChatMessage
        assistant_message   saved assistant ChatMessage (with output attachments)
        user_attachments    list of uploaded file attachment records
        output_attachments  list of AI-generated file attachment records (downloadable)
    """
    authentication_classes = [SessionAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        # ── Validate conversation ownership ───────────────────────────────────
        try:
            conversation = ChatConversation.objects.get(
                pk=conversation_id, user=request.user
            )
        except ChatConversation.DoesNotExist:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        user_input = (request.data.get("message") or "").strip()
        uploaded_files = request.FILES.getlist("files")

        if not user_input and not uploaded_files:
            return Response(
                {"error": "Provide a message or at least one file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user_input and uploaded_files:
            user_input = "Please extract all data from the attached file and create an Excel file."

        # ── Save user message ─────────────────────────────────────────────────
        user_msg = ChatMessage.objects.create(
            conversation=conversation,
            role="user",
            content=user_input,
        )

        # ── Save uploaded file attachments ────────────────────────────────────
        user_attachments = []
        for f in uploaded_files:
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else "other"
            att = ChatMessageAttachment.objects.create(
                message=user_msg,
                file=f,
                filename=f.name,
                file_type=ext,
                attachment_type="user_upload",
                file_size_bytes=f.size,
            )
            user_attachments.append(att)

        # ── Build conversation history for multi-turn context ─────────────────
        history = list(
            ChatMessage.objects.filter(conversation=conversation)
            .exclude(pk=user_msg.pk)
            .order_by("created_at")
            .values("role", "content")
        )[-20:]   # last 20 turns — keeps token usage bounded
        conv_history = [{"role": m["role"], "content": m["content"]} for m in history]

        # ── Get AI response ───────────────────────────────────────────────────
        workflow_id = request.data.get("workflow_id")
        workflow_option = workflow_id if workflow_id and str(workflow_id).startswith("full_") else None
        workflow_id_int = None
        if workflow_id and not workflow_option:
            try:
                workflow_id_int = int(workflow_id)
            except (TypeError, ValueError):
                workflow_id_int = None

        try:
            response_text, output_files = ChatService.get_response(
                message=user_input,
                user=request.user,
                file_attachments=user_attachments if user_attachments else None,
                workflow_id=workflow_id_int,
                workflow_option=workflow_option,
                conversation_history=conv_history,
            )
        except Exception as exc:
            logger.exception("ChatService.get_response failed: %s", exc)
            return Response(
                {"error": f"Failed to generate response: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── Save assistant message ────────────────────────────────────────────
        applied_workflow = None
        if workflow_id and not workflow_option:
            try:
                applied_workflow = Workflow.objects.get(pk=workflow_id_int)
            except Workflow.DoesNotExist:
                pass

        assistant_msg = ChatMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content=response_text,
            applied_workflow=applied_workflow,
        )

        # ── Save AI-generated output files as attachments ─────────────────────
        output_attachments = []
        for out in output_files:
            file_content = out["content"].read()
            att = ChatMessageAttachment(
                message=assistant_msg,
                filename=out["filename"],
                file_type=out["filename"].rsplit(".", 1)[-1].lower(),
                attachment_type="assistant_output",
                file_size_bytes=len(file_content),
            )
            att.file.save(out["filename"], ContentFile(file_content), save=True)
            output_attachments.append(att)

        # ── Auto-title conversation ───────────────────────────────────────────
        if conversation.title in ("Untitled Conversation", ""):
            conversation.title = user_input[:50]
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["title", "updated_at"])
        else:
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])

        ctx = {"request": request}
        return Response(
            {
                "user_message":      ChatMessageSerializer(user_msg, context=ctx).data,
                "assistant_message": ChatMessageSerializer(assistant_msg, context=ctx).data,
                "user_attachments":  ChatMessageAttachmentSerializer(user_attachments, many=True, context=ctx).data,
                "output_attachments": ChatMessageAttachmentSerializer(output_attachments, many=True, context=ctx).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Message list
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessageListView(generics.ListAPIView):
    """GET /api/chat/conversations/<conversation_id>/messages/"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChatMessageSerializer
    pagination_class = None

    def get_queryset(self):
        try:
            conv = ChatConversation.objects.get(
                pk=self.kwargs["conversation_id"], user=self.request.user
            )
            return ChatMessage.objects.filter(conversation=conv).order_by("created_at")
        except ChatConversation.DoesNotExist:
            return ChatMessage.objects.none()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Attachment download
# ─────────────────────────────────────────────────────────────────────────────

class ChatAttachmentDownloadView(APIView):
    """
    GET /api/chat/attachments/<id>/download/

    Streams the file to the client.
    Works for both user-uploaded files and AI-generated output files.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, attachment_id):
        try:
            att = ChatMessageAttachment.objects.select_related(
                "message__conversation"
            ).get(pk=attachment_id)
        except ChatMessageAttachment.DoesNotExist:
            return Response({"error": "Attachment not found."}, status=status.HTTP_404_NOT_FOUND)

        if att.message.conversation.user != request.user:
            return Response({"error": "Access denied."}, status=status.HTTP_403_FORBIDDEN)

        if not att.file:
            return Response({"error": "File not available."}, status=status.HTTP_404_NOT_FOUND)

        content_types = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf":  "application/pdf",
            "csv":  "text/csv",
            "txt":  "text/plain",
        }
        ct = content_types.get(att.file_type, "application/octet-stream")

        response = FileResponse(att.file.open("rb"), content_type=ct, as_attachment=True)
        response["Content-Disposition"] = f'attachment; filename="{att.filename}"'
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Workflows
# ─────────────────────────────────────────────────────────────────────────────

class WorkflowViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/chat/workflows/           — all enabled workflows
    GET /api/chat/workflows/defaults/  — default workflows for sidebar
    GET /api/chat/workflows/?type=...  — filter by workflow_type
    """
    serializer_class = WorkflowSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Workflow.objects.filter(enabled=True)
        wf_type = self.request.query_params.get("type")
        if wf_type:
            qs = qs.filter(workflow_type=wf_type)
        return qs

    @action(detail=False, methods=["get"])
    def defaults(self, request):
        qs = Workflow.objects.filter(enabled=True, is_default=True)
        return Response(WorkflowSerializer(qs, many=True).data)


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/chat/message/  — stateless single-turn endpoint
# ─────────────────────────────────────────────────────────────────────────────

class ChatSimpleMessageView(APIView):
    """
    POST /api/chat/message/

    Stateless single-turn message matching the frontend API contract.
    Accepts JSON body:
        {
            "message": "string",
            "conversation_history": [...],
            "file_metadata": [{"name", "size", "type", "content"}]
        }

    Returns:
        { "response": "string", "processed_data": {...} | null }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        message = (request.data.get("message") or "").strip()
        history = request.data.get("conversation_history") or []
        file_metadata = request.data.get("file_metadata") or []

        if not message and not file_metadata:
            return Response(
                {"error": {"code": "bad_request", "message": "Provide a message or file_metadata.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not message and file_metadata:
            message = "Please extract all data from the attached file and create an Excel file."

        # Build enriched message with file content snippets
        enriched = message
        if file_metadata:
            sections = []
            for fm in file_metadata:
                name    = fm.get("name", "file")
                content = fm.get("content", "")
                if content:
                    sections.append(f"[FILE: {name}]\n{content[:5000]}")
            if sections:
                enriched = message + "\n\n=== ATTACHED FILE CONTENT ===\n" + "\n\n---\n\n".join(sections)

        try:
            response_text, output_files = ChatService.get_response(
                message=enriched,
                user=request.user,
                conversation_history=history,
            )
        except Exception as exc:
            logger.exception("ChatSimpleMessageView failed: %s", exc)
            return Response(
                {"error": {"code": "server_error", "message": str(exc), "details": None}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        processed_data = None
        if output_files:
            first = output_files[0]
            processed_data = {
                "format":   first["filename"].rsplit(".", 1)[-1] if "." in first["filename"] else "xlsx",
                "filename": first["filename"],
                "data":     [],
            }

        return Response({
            "response":       response_text,
            "processed_data": processed_data,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Contract: GET /api/chat/history
# ─────────────────────────────────────────────────────────────────────────────

class ChatHistoryView(APIView):
    """
    GET /api/chat/history?conversationId=<id>&limit=50

    Returns messages for a conversation (or the most recent conversation).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        conversation_id = request.query_params.get("conversationId")
        limit = int(request.query_params.get("limit", 50))

        try:
            if conversation_id:
                conv = ChatConversation.objects.get(pk=conversation_id, user=request.user)
            else:
                conv = ChatConversation.objects.filter(user=request.user).order_by("-updated_at").first()
                if not conv:
                    return Response({"messages": []})
        except ChatConversation.DoesNotExist:
            return Response(
                {"error": {"code": "not_found", "message": "Conversation not found.", "details": None}},
                status=status.HTTP_404_NOT_FOUND,
            )

        messages = (
            ChatMessage.objects.filter(conversation=conv)
            .order_by("created_at")
            .prefetch_related("attachments")
        )[:limit]

        ctx = {"request": request}
        return Response({
            "messages": ChatMessageSerializer(messages, many=True, context=ctx).data
        })

# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/chat/process-file/
# ─────────────────────────────────────────────────────────────────────────────

class ChatProcessFileView(APIView):
    """
    POST /api/chat/process-file/
    Process an uploaded file and return structured data.

    multipart/form-data:
        file          File object
        action        convert | analyze | validate | clean
        output_format csv | json | xlsx | pdf | txt
    """
    permission_classes = [permissions.IsAuthenticated]

    from rest_framework.parsers import MultiPartParser, FormParser
    parser_classes = [MultiPartParser, FormParser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        import base64, csv, json as _json
        from io import BytesIO, StringIO
        from pathlib import Path

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response(
                {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        action        = request.data.get("action", "convert")
        output_format = request.data.get("output_format", "xlsx").lower()
        filename      = uploaded.name
        ext           = Path(filename).suffix.lstrip(".").lower()

        warnings  = []
        errors    = 0
        rows_processed = 0
        content   = b""
        out_filename = Path(filename).stem + f"_processed.{output_format}"

        try:
            from uploads.services import UploadService
            from uploads.models import UploadBatch

            # Ingest into a temporary batch for text extraction
            batch = UploadBatch.objects.create(
                label=f"process-file-{filename}",
                uploaded_by=request.user,
            )
            record = UploadService.ingest_file(batch, uploaded)
            extracted_text = record.extracted_text or ""
            rows_processed = extracted_text.count("\n") + 1 if extracted_text else 0

            if output_format == "xlsx":
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Data"
                lines = [l for l in extracted_text.splitlines() if l.strip()]
                for i, line in enumerate(lines):
                    ws.cell(row=i + 1, column=1, value=line)
                buf = BytesIO()
                wb.save(buf)
                content = buf.getvalue()

            elif output_format == "csv":
                lines = [l for l in extracted_text.splitlines() if l.strip()]
                buf = StringIO()
                writer = csv.writer(buf)
                for line in lines:
                    writer.writerow([line])
                content = buf.getvalue().encode("utf-8")

            elif output_format == "json":
                lines = [l for l in extracted_text.splitlines() if l.strip()]
                content = _json.dumps({"rows": lines}, indent=2).encode("utf-8")

            elif output_format == "txt":
                content = extracted_text.encode("utf-8")

            else:
                content = extracted_text.encode("utf-8")
                warnings.append(f"Unsupported output_format '{output_format}', returned as txt.")

        except Exception as exc:
            logger.exception("ChatProcessFileView failed: %s", exc)
            errors += 1
            warnings.append(str(exc))
            content = b""

        mime_map = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv":  "text/csv",
            "json": "application/json",
            "txt":  "text/plain",
            "pdf":  "application/pdf",
        }

        return Response({
            "status": "success" if not errors else "error",
            "processed_file": {
                "filename": out_filename,
                "format":   output_format,
                "size":     len(content),
                "content":  base64.b64encode(content).decode("utf-8") if content else "",
            },
            "summary": {
                "rows_processed": rows_processed,
                "errors":         errors,
                "warnings":       warnings,
            },
        })


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/chat/convert-format/
# ─────────────────────────────────────────────────────────────────────────────

class ChatConvertFormatView(APIView):
    """
    POST /api/chat/convert-format/
    Convert a file between formats.

    multipart/form-data:
        file         File object
        from_format  txt | csv | json | xlsx | xls | pdf | docx
        to_format    txt | csv | json | xlsx | pdf | docx
    """
    permission_classes = [permissions.IsAuthenticated]

    from rest_framework.parsers import MultiPartParser, FormParser
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        import base64, csv, json as _json
        from io import BytesIO, StringIO
        from pathlib import Path

        uploaded    = request.FILES.get("file")
        to_format   = (request.data.get("to_format") or "xlsx").lower()

        if not uploaded:
            return Response(
                {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filename    = uploaded.name
        out_name    = Path(filename).stem + f".{to_format}"
        content     = b""

        mime_map = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv":  "text/csv",
            "json": "application/json",
            "txt":  "text/plain",
            "pdf":  "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        try:
            from uploads.services import UploadService
            from uploads.models import UploadBatch

            batch  = UploadBatch.objects.create(
                label=f"convert-{filename}",
                uploaded_by=request.user,
            )
            record = UploadService.ingest_file(batch, uploaded)
            text   = record.extracted_text or ""
            lines  = [l for l in text.splitlines() if l.strip()]

            if to_format == "xlsx":
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Converted"
                for i, line in enumerate(lines, 1):
                    # Try to split on tab or comma for structured data
                    parts = line.split("\t") if "\t" in line else line.split(",")
                    for j, part in enumerate(parts, 1):
                        ws.cell(row=i, column=j, value=part.strip())
                buf = BytesIO()
                wb.save(buf)
                content = buf.getvalue()

            elif to_format == "csv":
                buf = StringIO()
                writer = csv.writer(buf)
                for line in lines:
                    parts = line.split("\t") if "\t" in line else [line]
                    writer.writerow(parts)
                content = buf.getvalue().encode("utf-8")

            elif to_format == "json":
                content = _json.dumps({"rows": lines}, indent=2).encode("utf-8")

            elif to_format == "txt":
                content = text.encode("utf-8")

            else:
                content = text.encode("utf-8")

        except Exception as exc:
            logger.exception("ChatConvertFormatView failed: %s", exc)
            return Response(
                {"error": {"code": "server_error", "message": str(exc), "details": None}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "filename":  out_name,
            "format":    to_format,
            "content":   base64.b64encode(content).decode("utf-8"),
            "mime_type": mime_map.get(to_format, "application/octet-stream"),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/chat/export-data/
# ─────────────────────────────────────────────────────────────────────────────

class ChatExportDataView(APIView):
    """
    POST /api/chat/export-data/
    Export in-memory data to a file format.

    JSON body:
        {
            "data": [...] or {...},
            "format": "csv|json|xlsx|pdf|txt",
            "filename": "output",
            "options": { "include_headers": true, "delimiter": "," }
        }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        import base64, csv, json as _json
        from io import BytesIO, StringIO

        data        = request.data.get("data")
        fmt         = (request.data.get("format") or "xlsx").lower()
        filename    = (request.data.get("filename") or "export").rstrip(".")
        options     = request.data.get("options") or {}
        include_hdr = options.get("include_headers", True)
        delimiter   = options.get("delimiter", ",")

        if data is None:
            return Response(
                {"error": {"code": "bad_request", "message": "data is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        out_name = f"{filename}.{fmt}"
        content  = b""

        mime_map = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv":  "text/csv",
            "json": "application/json",
            "txt":  "text/plain",
            "pdf":  "application/pdf",
        }

        try:
            rows = data if isinstance(data, list) else [data]

            if fmt == "json":
                content = _json.dumps(data, indent=2, default=str).encode("utf-8")

            elif fmt in ("csv", "txt"):
                buf = StringIO()
                writer = csv.writer(buf, delimiter=delimiter if fmt == "csv" else "\t")
                if rows and isinstance(rows[0], dict):
                    if include_hdr:
                        writer.writerow(rows[0].keys())
                    for row in rows:
                        writer.writerow(row.values())
                elif rows and isinstance(rows[0], list):
                    for row in rows:
                        writer.writerow(row)
                else:
                    for row in rows:
                        writer.writerow([str(row)])
                content = buf.getvalue().encode("utf-8")

            elif fmt == "xlsx":
                import openpyxl
                from openpyxl.styles import Font, PatternFill, Alignment

                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Export"

                start_row = 1
                if rows and isinstance(rows[0], dict) and include_hdr:
                    headers = list(rows[0].keys())
                    for col, h in enumerate(headers, 1):
                        cell = ws.cell(row=1, column=col, value=str(h))
                        cell.font = Font(bold=True, color="FFFFFF")
                        cell.fill = PatternFill("solid", fgColor="1F4E79")
                    start_row = 2

                for r_idx, row in enumerate(rows, start_row):
                    if isinstance(row, dict):
                        for c_idx, val in enumerate(row.values(), 1):
                            ws.cell(row=r_idx, column=c_idx, value=val)
                    elif isinstance(row, list):
                        for c_idx, val in enumerate(row, 1):
                            ws.cell(row=r_idx, column=c_idx, value=val)
                    else:
                        ws.cell(row=r_idx, column=1, value=str(row))

                buf = BytesIO()
                wb.save(buf)
                content = buf.getvalue()

            else:
                # Fallback: plain text
                content = _json.dumps(data, indent=2, default=str).encode("utf-8")

        except Exception as exc:
            logger.exception("ChatExportDataView failed: %s", exc)
            return Response(
                {"error": {"code": "server_error", "message": str(exc), "details": None}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "filename":  out_name,
            "format":    fmt,
            "content":   base64.b64encode(content).decode("utf-8"),
            "mime_type": mime_map.get(fmt, "application/octet-stream"),
        })
