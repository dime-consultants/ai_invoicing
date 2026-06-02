# uploads/serializers.py
from rest_framework import serializers
from .models import UploadBatch, UploadedFile


class UploadedFileSerializer(serializers.ModelSerializer):
    """Serializer for a single file within a batch."""
    status = serializers.CharField(source="parse_status", read_only=True)
    name   = serializers.CharField(source="original_filename", read_only=True)
    size   = serializers.IntegerField(source="file_size_bytes", read_only=True)
    type   = serializers.CharField(source="mime_type", read_only=True)
    uploadedAt = serializers.DateTimeField(source="uploaded_at", read_only=True)
    uploadedBy = serializers.SerializerMethodField()

    class Meta:
        model  = UploadedFile
        fields = [
            "id", "name", "type", "size",
            "extension", "detected_type", "detection_confidence",
            "status", "parse_error",
            "uploadedAt", "uploadedBy",
        ]
        read_only_fields = fields

    def get_uploadedBy(self, obj):
        if obj.batch and obj.batch.uploaded_by:
            return obj.batch.uploaded_by.get_full_name() or obj.batch.uploaded_by.username
        return None


class UploadBatchSerializer(serializers.ModelSerializer):
    """Full batch detail including nested files."""
    files      = UploadedFileSerializer(many=True, read_only=True)
    uploadedBy = serializers.SerializerMethodField()

    class Meta:
        model  = UploadBatch
        fields = [
            "id", "label", "description", "status",
            "file_count", "processed_count", "error_count",
            "uploadedBy",
            "created_at", "updated_at",
            "files",
        ]
        read_only_fields = [
            "id", "status", "file_count", "processed_count",
            "error_count", "created_at", "updated_at", "files",
        ]

    def get_uploadedBy(self, obj):
        if obj.uploaded_by:
            return obj.uploaded_by.get_full_name() or obj.uploaded_by.username
        return None


class UploadBatchListSerializer(serializers.ModelSerializer):
    """Lightweight batch list — no nested files."""
    uploadedBy = serializers.SerializerMethodField()

    class Meta:
        model  = UploadBatch
        fields = [
            "id", "label", "description", "status",
            "file_count", "processed_count", "error_count",
            "uploadedBy", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_uploadedBy(self, obj):
        if obj.uploaded_by:
            return obj.uploaded_by.get_full_name() or obj.uploaded_by.username
        return None


class FileUploadSerializer(serializers.Serializer):
    """Used for POST /api/files/upload — multipart file upload."""
    file = serializers.FileField()
    type = serializers.CharField(required=False, allow_blank=True, default="")
