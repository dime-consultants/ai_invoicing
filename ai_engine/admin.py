# ai_engine/admin.py
from django.contrib import admin
from django.utils.html import format_html

from .models import AIAnalysisJob, AIInsight


class AIInsightInline(admin.TabularInline):
    model   = AIInsight
    extra   = 0
    fields  = (
        "insight_type", "severity", "reference_key",
        "title", "is_actioned",
    )
    readonly_fields = fields
    show_change_link = True
    can_delete = False
    ordering = ("-severity", "created_at")


class ToolCallInline(admin.TabularInline):
    """
    Shows all tool calls made during a job.
    Imported from tools.admin to avoid duplication — but defined
    here minimally so ai_engine.admin has no hard dependency on tools.
    """
    # Lazy import avoids circular-import at module load time
    @property
    def model(self):
        from tools.models import ToolCall
        return ToolCall

    extra   = 0
    fields  = ("tool", "status", "arguments", "result", "error_message", "duration_ms_display")
    readonly_fields = fields
    show_change_link = True
    can_delete = False

    @admin.display(description="Duration (ms)")
    def duration_ms_display(self, obj):
        ms = obj.duration_ms
        return f"{ms} ms" if ms is not None else "—"


# ── AIAnalysisJob ─────────────────────────────────────────────────────────────

@admin.register(AIAnalysisJob)
class AIAnalysisJobAdmin(admin.ModelAdmin):
    list_display  = (
        "id", "task_type", "batch", "target_file",
        "requested_by", "status_badge", "total_tokens",
        "duration_display", "created_at",
    )
    list_filter   = ("task_type", "status", "created_at")
    search_fields = (
        "batch__label", "requested_by__username",
        "user_intent", "error_message",
    )
    readonly_fields = (
        "status", "raw_response", "structured_output",
        "input_tokens", "output_tokens",
        "error_message", "started_at", "finished_at", "created_at",
    )
    ordering = ("-created_at",)
    # Tools inline requires tools app — add only if installed
    inlines = [AIInsightInline]

    fieldsets = (
        ("Job", {
            "fields": (
                "batch", "target_file", "requested_by",
                "task_type", "status",
            ),
        }),
        ("Intent", {
            "fields": ("user_intent",),
        }),
        ("Prompts", {
            "fields": ("system_prompt", "user_prompt"),
            "classes": ("collapse",),
        }),
        ("Response", {
            "fields": ("raw_response", "structured_output"),
            "classes": ("collapse",),
        }),
        ("Tokens", {
            "fields": ("input_tokens", "output_tokens"),
            "classes": ("collapse",),
        }),
        ("Error", {
            "fields": ("error_message",),
            "classes": ("collapse",),
        }),
        ("Timing", {
            "fields": ("started_at", "finished_at", "created_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "queued":  "#6c757d",
            "running": "#0d6efd",
            "done":    "#198754",
            "error":   "#dc3545",
        }
        colour = colours.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:0.8em">{}</span>',
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="Duration")
    def duration_display(self, obj):
        s = obj.duration_seconds
        if s is None:
            return "—"
        if s < 60:
            return f"{s:.1f}s"
        return f"{int(s // 60)}m {int(s % 60)}s"


# ── AIInsight ─────────────────────────────────────────────────────────────────

@admin.register(AIInsight)
class AIInsightAdmin(admin.ModelAdmin):
    list_display  = (
        "id", "severity_badge", "insight_type",
        "reference_key", "title", "job",
        "is_actioned", "created_at",
    )
    list_filter   = ("severity", "insight_type", "is_actioned", "created_at")
    search_fields = ("title", "detail", "reference_key", "job__id")
    readonly_fields = (
        "job", "source_tool_call",
        "insight_type", "severity", "reference_key",
        "title", "detail",
        "is_actioned", "actioned_by", "actioned_at",
        "created_at",
    )
    ordering = ("-severity", "-created_at")

    fieldsets = (
        ("Insight", {
            "fields": (
                "job", "source_tool_call",
                "insight_type", "severity", "reference_key",
                "title", "detail",
            ),
        }),
        ("Resolution", {
            "fields": (
                "is_actioned", "actioned_by",
                "actioned_at", "resolution_note",
            ),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Severity")
    def severity_badge(self, obj):
        colours = {
            "info":     "#0d6efd",
            "warning":  "#fd7e14",
            "critical": "#dc3545",
        }
        colour = colours.get(obj.severity, "#6c757d")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:0.8em">{}</span>',
            colour,
            obj.get_severity_display(),
        )
