# chat/serializers.py
from django.urls import reverse
from rest_framework import serializers
from .models import ChatConversation, ChatMessage, ChatMessageAttachment, Workflow


def _get_user_display(user):
    if not user:
        return None
    full = f"{user.first_name} {user.last_name}".strip()
    return full or user.email


class ChatMessageAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessageAttachment
        fields = [
            "id", "filename", "file_type", "attachment_type",
            "file_size_bytes", "created_at", "file_url", "download_url"
        ]
        read_only_fields = ["created_at"]

    def get_file_url(self, obj):
        request = self.context.get('request')
        if obj.file and request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url if obj.file else None

    def get_download_url(self, obj):
        request = self.context.get('request')
        url = reverse("attachment_download", kwargs={"attachment_id": obj.pk})
        if request:
            return request.build_absolute_uri(url)
        return url


class ChatMessageSerializer(serializers.ModelSerializer):
    attachments = ChatMessageAttachmentSerializer(many=True, read_only=True)
    applied_workflow_name = serializers.CharField(
        source='applied_workflow.name', read_only=True
    )

    class Meta:
        model = ChatMessage
        fields = [
            "id", "role", "content", "created_at",
            "applied_workflow", "applied_workflow_name", "attachments"
        ]
        read_only_fields = ["created_at", "applied_workflow"]


class WorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workflow
        fields = [
            "id", "name", "description", "workflow_type", "steps",
            "enabled", "is_default", "created_at", "updated_at"
        ]
        read_only_fields = ["created_at", "updated_at"]


class ChatConversationListSerializer(serializers.ModelSerializer):
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = ChatConversation
        fields = ["id", "title", "created_at", "updated_at", "message_count"]
        read_only_fields = ["created_at", "updated_at"]

    def get_message_count(self, obj):
        return obj.messages.count()


class ChatConversationSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = ChatConversation
        fields = ["id", "title", "created_at", "updated_at", "message_count", "messages"]
        read_only_fields = ["created_at", "updated_at"]

    def get_message_count(self, obj):
        return obj.messages.count()