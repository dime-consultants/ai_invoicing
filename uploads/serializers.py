from rest_framework import serializers
from .models import UploadBatch, UploadedFile


def _get_user_display(user):
    """Safe display name for custom User model without get_full_name()."""
    if not user:
        return None
    full = f"{user.first_name} {user.last_name}".strip()
    return full or getattr(user, "email", None) or str(user)


class UploadedFileSerializer(serializers.ModelSerializer):
    status              = serializers.CharField(source="parse_status", read_only=True)
    name                = serializers.CharField(source="original_filename", read_only=True)
    size                = serializers.IntegerField(source="file_size_bytes", read_only=True)
    type                = serializers.CharField(source="mime_type", read_only=True)
    uploadedAt          = serializers.DateTimeField(source="uploaded_at", read_only=True)
    uploadedBy          = serializers.SerializerMethodField()
    pageCount           = serializers.IntegerField(source="page_count", read_only=True)
    extractionDeferred  = serializers.BooleanField(source="extraction_deferred", read_only=True)

    class Meta:
        model  = UploadedFile
        fields = [
            "id", "public_id", "name", "type", "size",
            "extension", "checksum_sha256", "detected_type", "detection_confidence",
            "status", "parse_error",
            "pageCount", "extractionDeferred",
            "uploadedAt", "uploadedBy",
        ]
        read_only_fields = fields

    def get_uploadedBy(self, obj):
        user = obj.batch.uploaded_by if obj.batch else None
        return _get_user_display(user)


class UploadBatchSerializer(serializers.ModelSerializer):
    files      = UploadedFileSerializer(many=True, read_only=True)
    uploadedBy = serializers.SerializerMethodField()

    class Meta:
        model  = UploadBatch
        fields = [
            "id", "public_id", "label", "description", "status",
            "file_count", "processed_count", "error_count",
            "uploadedBy",
            "created_at", "updated_at",
            "files",
        ]
        read_only_fields = [
            "id", "public_id", "status", "file_count", "processed_count",
            "error_count", "created_at", "updated_at", "files",
        ]

    def get_uploadedBy(self, obj):
        return _get_user_display(obj.uploaded_by)


class UploadBatchListSerializer(serializers.ModelSerializer):
    uploadedBy = serializers.SerializerMethodField()

    class Meta:
        model  = UploadBatch
        fields = [
            "id", "public_id", "label", "description", "status",
            "file_count", "processed_count", "error_count",
            "uploadedBy", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_uploadedBy(self, obj):
        return _get_user_display(obj.uploaded_by)


class FileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    type = serializers.CharField(required=False, allow_blank=True, default="")
