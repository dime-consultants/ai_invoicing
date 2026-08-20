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
            "id", "public_id", "name", "type", "status", "format",
            "fileSize", "generatedAt", "created_at",
        ]
        read_only_fields = fields


class ReportParametersSerializer(serializers.Serializer):
    """Validated sub-fields of ReportGenerateSerializer.parameters — narrows
    which data a report covers. All optional; an empty {} covers everything
    the requesting org has."""
    batch_id  = serializers.IntegerField(required=False, allow_null=True)
    date_from = serializers.DateField(required=False, allow_null=True)
    date_to   = serializers.DateField(required=False, allow_null=True)


class ReportGenerateSerializer(serializers.Serializer):
    """Used for POST /api/reports/generate"""
    type       = serializers.ChoiceField(choices=["reconciliation", "billing", "analytics", "custom"])
    parameters = ReportParametersSerializer(required=False, default=dict)
    format     = serializers.ChoiceField(choices=["pdf", "xlsx", "csv"], default="pdf")

    def validate_parameters(self, value):
        return dict(value) if value else {}
