# tools/serializers.py
from rest_framework import serializers
from .models import ToolDefinition, ToolCall


class ToolDefinitionSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    grok_schema = serializers.SerializerMethodField()

    class Meta:
        model  = ToolDefinition
        fields = [
            "id", "name", "display_name", "description",
            "category", "category_display",
            "parameters_schema", "handler",
            "version", "enabled", "is_safe",
            "grok_schema",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_grok_schema(self, obj):
        return obj.to_grok_schema()


class ToolCallSerializer(serializers.ModelSerializer):
    tool_name    = serializers.CharField(source="tool.name",         read_only=True)
    tool_display = serializers.CharField(source="tool.display_name", read_only=True)
    duration_ms  = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model  = ToolCall
        fields = [
            "id", "tool", "tool_name", "tool_display",
            "job",
            "arguments", "result", "error_message",
            "status", "status_display",
            "duration_ms",
            "started_at", "finished_at", "created_at",
        ]
        read_only_fields = fields