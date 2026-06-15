# chat/views.py
import base64
import csv
import json
import logging
from io import BytesIO, StringIO
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.utils import timezone
from django.views.generic import TemplateView

from rest_framework import generics, permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
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

MIME_MAP = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf":  "application/pdf",
    "csv":  "text/csv",
    "txt":  "text/plain",
    "json": "application/json",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_workflow_id(raw):
    """
    Return (workflow_id_int, workflow_option) from a raw workflow_id value.
    workflow_option is set when the value starts with 'full_' (e.g. 'full_pipeline').
    """
    if not raw:
        return None, None
    raw = str(raw)
    if raw.startswith("full_"):
        return None, raw
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, None


def _save_output_attachments(output_files, assistant_msg):
    """
    Persist tool-produced output files as ChatMessageAttachment records.
    Returns the list of saved attachment instances.
    """
    saved = []
    for out in output_files:
        buf = out["content"]
        # Always rewind — ToolService leaves the cursor at the end
        if hasattr(buf, "seek"):
            buf.seek(0)
        file_content = buf.read()
        if not file_content:
            logger.warning("Output file %s is empty — skipping.", out["filename"])
            continue

        ext = out["filename"].rsplit(".", 1)[-1].lower() if "." in out["filename"] else "bin"
        att = ChatMessageAttachment(
            message=assistant_msg,
            filename=out["filename"],
            file_type=ext,
            attachment_type="assistant_output",
            file_size_bytes=len(file_content),
        )
        att.file.save(out["filename"], ContentFile(file_content), save=True)
        saved.append(att)
    return saved


def _build_conv_history(conversation, exclude_pk):
    """Return the last 20 turns of a conversation as a list of role/content dicts."""
    rows = (
        ChatMessage.objects
        .filter(conversation=conversation)
        .exclude(pk=exclude_pk)
        .order_by("created_at")
        .values("role", "content")
    )[-20:]
    return [{"role": m["role"], "content": m["content"]} for m in rows]


def _auto_title(conversation, user_input):
    """Generate and save a conversation title if it is still the default."""
    if conversation.title not in ("Untitled Conversation", ""):
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])
        return

    from .title_generator import generate_title_from_user_input
    conversation.title = generate_title_from_user_input(user_input) or user_input[:50]
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=["title", "updated_at"])


# ─────────────────────────────────────────────────────────────────────────────
# UI entry point
# ─────────────────────────────────────────────────────────────────────────────

class ChatInterfaceView(TemplateView):
    """Serve the invoice processing chat UI."""
    template_name = "chat/interface.html"


# ─────────────────────────────────────────────────────────────────────────────
# Conversations
# ─────────────────────────────────────────────────────────────────────────────

class ChatConversationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/chat/conversations/  — list user's conversations (no messages)
    POST /api/chat/conversations/  — create a new conversation
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        return ChatConversationListSerializer if self.request.method == "GET" else ChatConversationSerializer

    def get_queryset(self):
        return ChatConversation.objects.filter(user=self.request.user).order_by("-updated_at")

    def create(self, request, *args, **kwargs):
        data = request.data
        if isinstance(data, dict):
            title = (data.get("title") or "").strip()
        elif isinstance(data, str):
            title = data.strip()
        else:
            title = ""
        title = title or "Untitled Conversation"

        conv = ChatConversation.objects.create(user=request.user, title=title)
        return Response(ChatConversationSerializer(conv).data, status=status.HTTP_201_CREATED)


class ChatConversationDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/chat/conversations/<id>/
    PATCH  /api/chat/conversations/<id>/  — title / is_active only
    DELETE /api/chat/conversations/<id>/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChatConversationSerializer

    def get_queryset(self):
        return ChatConversation.objects.filter(user=self.request.user)

    def update(self, request, *args, **kwargs):
        allowed = {"title", "is_active"}
        data = request.data
        if isinstance(data, dict):
            updates = {k: v for k, v in data.items() if k in allowed}
        elif isinstance(data, str):
            updates = {"title": data.strip()} if data.strip() else {}
        else:
            updates = {}

        instance = self.get_object()
        for field, value in updates.items():
            setattr(instance, field, value)
        if updates:
            instance.save(update_fields=list(updates.keys()) + ["updated_at"])
        return Response(ChatConversationSerializer(instance).data)


# ─────────────────────────────────────────────────────────────────────────────
# Send message  (primary chat endpoint)
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessageSendView(APIView):
    """
    POST /api/chat/conversations/<conversation_id>/send/

    multipart/form-data:
        message      str       — required (or at least one file)
        files        file[]    — optional attachments
        workflow_id  int|str   — optional workflow selector

    Returns:
        user_message        saved user ChatMessage
        assistant_message   saved assistant ChatMessage
        user_attachments    uploaded file attachment records
        output_attachments  AI-generated output file records (downloadable)
    """
    authentication_classes = [SessionAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        try:
            conversation = ChatConversation.objects.get(pk=conversation_id, user=request.user)
        except ChatConversation.DoesNotExist:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        user_input     = (request.data.get("message") or "").strip()
        uploaded_files = request.FILES.getlist("files")

        if not user_input and not uploaded_files:
            return Response(
                {"error": "Provide a message or at least one file."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user_input:
            user_input = "Please extract all data from the attached file and create an Excel file."

        # ── Persist user message ──────────────────────────────────────────────
        user_msg = ChatMessage.objects.create(
            conversation=conversation,
            role="user",
            content=user_input,
        )

        # ── Persist uploaded files as ChatMessageAttachments ──────────────────
        # Raw attachment records — ChatService.get_response() will push them
        # through UploadService to get UploadedFile records with extracted_text.
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

        workflow_id_int, workflow_option = _parse_workflow_id(request.data.get("workflow_id"))

        try:
            response_text, output_files = ChatService.get_response(
                message=user_input,
                user=request.user,
                file_attachments=user_attachments or None,
                workflow_id=workflow_id_int,
                workflow_option=workflow_option,
                conversation_history=_build_conv_history(conversation, user_msg.pk),
            )
        except Exception as exc:
            logger.exception("ChatService.get_response failed: %s", exc)
            return Response(
                {"error": f"Failed to generate response: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── Persist assistant message ─────────────────────────────────────────
        applied_workflow = None
        if workflow_id_int:
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

        output_attachments = _save_output_attachments(output_files, assistant_msg)
        _auto_title(conversation, user_input)

        ctx = {"request": request}
        return Response(
            {
                "user_message":       ChatMessageSerializer(user_msg, context=ctx).data,
                "assistant_message":  ChatMessageSerializer(assistant_msg, context=ctx).data,
                "user_attachments":   ChatMessageAttachmentSerializer(user_attachments, many=True, context=ctx).data,
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
    Streams the file — works for user uploads and AI-generated output files.
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

        ct = MIME_MAP.get(att.file_type, "application/octet-stream")
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
# Stateless single-turn endpoint
# ─────────────────────────────────────────────────────────────────────────────

class ChatSimpleMessageView(APIView):
    """
    POST /api/chat/message/

    Stateless single-turn message. Files are ingested through UploadService
    (same path as the conversation endpoint) so tool handlers can reference
    them by file_id. The response includes a download URL for any output file.

    JSON body:
        {
            "message": "string",
            "conversation_history": [...],
        }

    multipart/form-data (alternative):
        message   str
        files     file[]

    Returns:
        { "response": "string", "processed_data": {...} | null }
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        message  = (request.data.get("message") or "").strip()
        history  = request.data.get("conversation_history") or []

        # Normalise history if it arrived as a JSON string (multipart sends strings)
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except Exception:
                history = []

        uploaded_files = request.FILES.getlist("files") or []

        if not message and not uploaded_files:
            return Response(
                {"error": {"code": "bad_request", "message": "Provide a message or files.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not message:
            message = "Please extract all data from the attached file and create an Excel file."

        # ── Ingest files through UploadService ────────────────────────────────
        # Create temporary ChatMessageAttachment records so ChatService can
        # push them through _ingest_attachments_to_upload_batch, giving every
        # file a proper UploadedFile record with extracted_text and file_id.
        temp_attachments = []
        if uploaded_files:
            # We need a throwaway ChatMessage to attach to; use a sentinel
            # conversation owned by this user (or create one on the fly).
            temp_conv, _ = ChatConversation.objects.get_or_create(
                user=request.user,
                title="__stateless__",
                defaults={"is_active": False},
            )
            temp_msg = ChatMessage.objects.create(
                conversation=temp_conv,
                role="user",
                content=message,
            )
            for f in uploaded_files:
                ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else "other"
                att = ChatMessageAttachment.objects.create(
                    message=temp_msg,
                    file=f,
                    filename=f.name,
                    file_type=ext,
                    attachment_type="user_upload",
                    file_size_bytes=f.size,
                )
                temp_attachments.append(att)

        try:
            response_text, output_files = ChatService.get_response(
                message=message,
                user=request.user,
                file_attachments=temp_attachments or None,
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
            buf = first["content"]
            if hasattr(buf, "seek"):
                buf.seek(0)
            ext = first["filename"].rsplit(".", 1)[-1] if "." in first["filename"] else "xlsx"
            processed_data = {
                "format":   ext,
                "filename": first["filename"],
                "content":  base64.b64encode(buf.read()).decode("utf-8"),
            }

        return Response({"response": response_text, "processed_data": processed_data})


# ─────────────────────────────────────────────────────────────────────────────
# Chat history
# ─────────────────────────────────────────────────────────────────────────────

class ChatHistoryView(APIView):
    """
    GET /api/chat/history?conversationId=<id>&limit=50
    Returns messages for a conversation (or the most recent one).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        conversation_id = request.query_params.get("conversationId")
        limit = min(int(request.query_params.get("limit", 50)), 200)

        try:
            if conversation_id:
                conv = ChatConversation.objects.get(pk=conversation_id, user=request.user)
            else:
                conv = (
                    ChatConversation.objects
                    .filter(user=request.user)
                    .exclude(title="__stateless__")
                    .order_by("-updated_at")
                    .first()
                )
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
        return Response({"messages": ChatMessageSerializer(messages, many=True, context=ctx).data})


# ─────────────────────────────────────────────────────────────────────────────
# Process file  — ingest + tool extraction, return base64 result
# ─────────────────────────────────────────────────────────────────────────────

class ChatProcessFileView(APIView):
    """
    POST /api/chat/process-file/

    Ingests a file through UploadService then runs ChatService so the tool
    handlers perform the actual extraction. Returns the output file as base64.

    multipart/form-data:
        file          File object  (required)
        action        convert | analyze | validate | clean  (ignored for now — tool decides)
        output_format xlsx | csv | json | txt
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response(
                {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        output_format = (request.data.get("output_format") or "xlsx").lower()

        # Ingest through UploadService via a temporary conversation + message
        from uploads.services import UploadService

        temp_conv, _ = ChatConversation.objects.get_or_create(
            user=request.user,
            title="__stateless__",
            defaults={"is_active": False},
        )
        temp_msg = ChatMessage.objects.create(
            conversation=temp_conv, role="user",
            content=f"Extract data from {uploaded.name}",
        )
        ext = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else "other"
        att = ChatMessageAttachment.objects.create(
            message=temp_msg,
            file=uploaded,
            filename=uploaded.name,
            file_type=ext,
            attachment_type="user_upload",
            file_size_bytes=uploaded.size,
        )

        warnings = []
        errors = 0
        content = b""
        out_filename = Path(uploaded.name).stem + f"_processed.{output_format}"

        try:
            response_text, output_files = ChatService.get_response(
                message=f"Extract all data from {uploaded.name} and return it as {output_format}.",
                user=request.user,
                file_attachments=[att],
            )

            if output_files:
                buf = output_files[0]["content"]
                if hasattr(buf, "seek"):
                    buf.seek(0)
                content = buf.read()
                out_filename = output_files[0]["filename"]
            else:
                warnings.append("No output file was produced by the extraction tools.")

        except Exception as exc:
            logger.exception("ChatProcessFileView failed: %s", exc)
            errors += 1
            warnings.append(str(exc))

        rows_processed = content.count(b"\n") + 1 if content else 0

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
# Convert format  — ingest + tool extraction to a specific target format
# ─────────────────────────────────────────────────────────────────────────────

class ChatConvertFormatView(APIView):
    """
    POST /api/chat/convert-format/

    multipart/form-data:
        file        File object  (required)
        to_format   xlsx | csv | json | txt
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded  = request.FILES.get("file")
        to_format = (request.data.get("to_format") or "xlsx").lower()

        if not uploaded:
            return Response(
                {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        out_name = Path(uploaded.name).stem + f".{to_format}"

        temp_conv, _ = ChatConversation.objects.get_or_create(
            user=request.user,
            title="__stateless__",
            defaults={"is_active": False},
        )
        temp_msg = ChatMessage.objects.create(
            conversation=temp_conv, role="user",
            content=f"Convert {uploaded.name} to {to_format}",
        )
        ext = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else "other"
        att = ChatMessageAttachment.objects.create(
            message=temp_msg,
            file=uploaded,
            filename=uploaded.name,
            file_type=ext,
            attachment_type="user_upload",
            file_size_bytes=uploaded.size,
        )

        try:
            _, output_files = ChatService.get_response(
                message=f"Convert {uploaded.name} to {to_format} format.",
                user=request.user,
                file_attachments=[att],
            )

            if not output_files:
                return Response(
                    {"error": {"code": "server_error", "message": "Conversion produced no output.", "details": None}},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            buf = output_files[0]["content"]
            if hasattr(buf, "seek"):
                buf.seek(0)
            content = buf.read()
            out_name = output_files[0]["filename"]

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
            "mime_type": MIME_MAP.get(to_format, "application/octet-stream"),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Export data  — serialise in-memory data to a file format
# ─────────────────────────────────────────────────────────────────────────────

class ChatExportDataView(APIView):
    """
    POST /api/chat/export-data/

    JSON body:
        {
            "data":     [...] | {...},
            "format":   "csv | json | xlsx | txt",
            "filename": "output",
            "options":  { "include_headers": true, "delimiter": "," }
        }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        data     = request.data.get("data")
        fmt      = (request.data.get("format") or "xlsx").lower()
        filename = (request.data.get("filename") or "export").rstrip(".")
        options  = request.data.get("options") or {}
        include_hdr = options.get("include_headers", True)
        delimiter   = options.get("delimiter", ",")

        if data is None:
            return Response(
                {"error": {"code": "bad_request", "message": "data is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        out_name = f"{filename}.{fmt}"
        content  = b""

        try:
            rows = data if isinstance(data, list) else [data]

            if fmt == "json":
                content = json.dumps(data, indent=2, default=str).encode("utf-8")

            elif fmt in ("csv", "txt"):
                sep = delimiter if fmt == "csv" else "\t"
                buf = StringIO()
                writer = csv.writer(buf, delimiter=sep)
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
                content = json.dumps(data, indent=2, default=str).encode("utf-8")

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
            "mime_type": MIME_MAP.get(fmt, "application/octet-stream"),
        })