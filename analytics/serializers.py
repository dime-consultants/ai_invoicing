# analytics/serializers.py
from rest_framework import serializers
from .models import Report


class ReportSerializer(serializers.ModelSerializer):
    """Serializer for the Report model."""
    type        = serializers.CharField(source="report_type", read_only=True)
    generatedAt = serializers.DateTimeField(source="generated_at", read_only=True)
    fileSize    = serializers.IntegerField(source="file_size", read_only=True)

    class Meta:
        model  = Report
        fields = [
            "id", "name", "type", "status", "format",
            "fileSize", "generatedAt", "created_at",
        ]
        read_only_fields = fields


class ReportGenerateSerializer(serializers.Serializer):
    """Used for POST /api/reports/generate"""
    type       = serializers.ChoiceField(choices=["reconciliation", "billing", "analytics", "custom"])
    parameters = serializers.DictField(required=False, default=dict)
    format     = serializers.ChoiceField(choices=["pdf", "xlsx", "csv"], default="pdf")
