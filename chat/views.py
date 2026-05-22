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
    #permission_classes = [permissions.IsAuthenticated]

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
    #permission_classes = [permissions.IsAuthenticated]
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
                attachment_type="ai_output",
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