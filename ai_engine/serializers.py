# ai_engine/serializers.py
from rest_framework import serializers
from .models import AIAnalysisJob, AIInsight


# ── AIInsight ─────────────────────────────────────────────────────────────────

class AIInsightSerializer(serializers.ModelSerializer):
    insight_type_display = serializers.CharField(
        source="get_insight_type_display", read_only=True
    )
    severity_display = serializers.CharField(
        source="get_severity_display", read_only=True
    )
    actioned_by_username = serializers.CharField(
        source="actioned_by.username", read_only=True, default=None
    )

    class Meta:
        model  = AIInsight
        fields = [
            "id",
            "insight_type", "insight_type_display",
            "severity",     "severity_display",
            "reference_key",
            "title", "detail",
            "source_tool_call",
            "is_actioned", "actioned_by_username", "actioned_at",
            "resolution_note",
            "created_at",
        ]
        read_only_fields = fields


# ── ToolCall summary (nested inside job) ─────────────────────────────────────

class ToolCallSummarySerializer(serializers.Serializer):
    """Lightweight read of a ToolCall — avoids importing tools serializers here."""
    id          = serializers.IntegerField(read_only=True)
    tool_name   = serializers.CharField(source="tool.name",         read_only=True)
    tool_display = serializers.CharField(source="tool.display_name", read_only=True)
    status      = serializers.CharField(read_only=True)
    duration_ms = serializers.IntegerField(read_only=True)
    arguments   = serializers.JSONField(read_only=True)
    result      = serializers.JSONField(read_only=True)
    error_message = serializers.CharField(read_only=True)
    created_at  = serializers.DateTimeField(read_only=True)


# ── AIAnalysisJob ─────────────────────────────────────────────────────────────

class AIAnalysisJobListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views — no nested insights or tool calls."""
    task_type_display  = serializers.CharField(source="get_task_type_display", read_only=True)
    status_display     = serializers.CharField(source="get_status_display",    read_only=True)
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )
    batch_label        = serializers.CharField(source="batch.label", read_only=True)
    target_filename    = serializers.CharField(
        source="target_file.original_filename", read_only=True, default=None
    )
    insight_count      = serializers.SerializerMethodField()
    tool_call_count    = serializers.SerializerMethodField()

    class Meta:
        model  = AIAnalysisJob
        fields = [
            "id",
            "task_type", "task_type_display",
            "status",    "status_display",
            "batch", "batch_label",
            "target_file", "target_filename",
            "requested_by_username",
            "user_intent",
            "input_tokens", "output_tokens", "total_tokens",
            "duration_seconds",
            "insight_count", "tool_call_count",
            "created_at", "started_at", "finished_at",
        ]
        read_only_fields = fields

    def get_insight_count(self, obj):
        return obj.insights.count()

    def get_tool_call_count(self, obj):
        return obj.tool_calls.count()


class AIAnalysisJobDetailSerializer(AIAnalysisJobListSerializer):
    """Full detail — includes nested insights and tool calls."""
    insights    = AIInsightSerializer(many=True, read_only=True)
    tool_calls  = ToolCallSummarySerializer(many=True, read_only=True)

    class Meta(AIAnalysisJobListSerializer.Meta):
        fields = AIAnalysisJobListSerializer.Meta.fields + [
            "system_prompt", "user_prompt",
            "raw_response", "structured_output",
            "error_message",
            "insights", "tool_calls",
        ]


class AIAnalysisJobCreateSerializer(serializers.ModelSerializer):
    """
    Used for POST /api/ai/jobs/ — creates and dispatches a job.
    The caller provides batch, task_type, and an optional user_intent.
    """
    class Meta:
        model  = AIAnalysisJob
        fields = [
            "batch", "target_file",
            "task_type",
            "user_intent",
            "system_prompt",   # optional override
        ]

    def validate_task_type(self, value):
        valid = {c[0] for c in AIAnalysisJob.TASK_TYPE_CHOICES}
        if value not in valid:
            raise serializers.ValidationError(
                f"Invalid task_type '{value}'. Choose from: {sorted(valid)}"
            )
        return value


# ── Action serializer (mark insight as actioned) ──────────────────────────────

class AIInsightActionSerializer(serializers.Serializer):
    resolution_note = serializers.CharField(required=False, allow_blank=True, default="")