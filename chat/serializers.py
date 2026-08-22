# chat/serializers.py
from django.urls import reverse
from rest_framework import serializers
from .models import ChatConversation, ChatMessage, ChatMessageAttachment, Workflow


class ChatMessageAttachmentSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model  = ChatMessageAttachment
        fields = [
            "id", "public_id", "filename", "file_type", "attachment_type",
            "file_size_bytes", "created_at",
            "download_url",
            "uploaded_file",   # FK to uploads.UploadedFile — useful for UI
        ]
        read_only_fields = fields

    def get_download_url(self, obj):
        request = self.context.get("request")
        url = reverse("attachment_download", kwargs={"attachment_id": obj.pk})
        return request.build_absolute_uri(url) if request else url


class ChatMessageSerializer(serializers.ModelSerializer):
    attachments           = ChatMessageAttachmentSerializer(many=True, read_only=True)
    applied_workflow_name = serializers.CharField(
        source="applied_workflow.name", read_only=True, default=None,
    )
    # Expose the AI job id + status so the UI can poll or show a spinner
    ai_job_id     = serializers.IntegerField(source="ai_job.pk",     read_only=True, default=None)
    ai_job_status = serializers.CharField(source="ai_job.status",   read_only=True, default=None)

    class Meta:
        model  = ChatMessage
        fields = [
            "id", "public_id", "role", "content", "created_at",
            "applied_workflow", "applied_workflow_name",
            "ai_job_id", "ai_job_status",
            "attachments",
        ]
        read_only_fields = fields


class WorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Workflow
        fields = [
            "id", "public_id", "name", "description", "workflow_type", "steps",
            "enabled", "is_default", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ChatConversationListSerializer(serializers.ModelSerializer):
    message_count  = serializers.SerializerMethodField()
    last_message   = serializers.SerializerMethodField()
    related_batch_label = serializers.CharField(
        source="related_batch.label", read_only=True, default=None,
    )

    class Meta:
        model  = ChatConversation
        fields = [
            "id", "public_id", "title", "is_active",
            "related_batch", "related_batch_label",
            "message_count", "last_message",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_message_count(self, obj):
        return obj.messages.count()

    def get_last_message(self, obj):
        msg = obj.messages.order_by("-created_at", "-pk").first()
        if not msg:
            return None
        return {
            "role":    msg.role,
            "content": msg.content[:120],
            "created_at": msg.created_at.isoformat(),
        }


class ChatConversationSerializer(serializers.ModelSerializer):
    messages      = ChatMessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()
    related_batch_label = serializers.CharField(
        source="related_batch.label", read_only=True, default=None,
    )

    class Meta:
        model  = ChatConversation
        fields = [
            "id", "public_id", "title", "is_active",
            "related_batch", "related_batch_label",
            "message_count", "messages",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_message_count(self, obj):
        return obj.messages.count()
