# tools/admin.py
from django.contrib import admin
from django.utils.html import format_html

from .models import ToolDefinition, ToolCall


# ── ToolDefinition ────────────────────────────────────────────────────────────

@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display  = (
        "name", "display_name", "category", "version",
        "enabled_badge", "is_safe", "updated_at",
    )
    list_filter   = ("category", "enabled", "is_safe")
    search_fields = ("name", "display_name", "description", "handler")
    ordering      = ("category", "name")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Identity", {
            "fields": ("name", "display_name", "description", "category", "version"),
        }),
        ("LLM Schema", {
            "fields": ("parameters_schema",),
            "description": (
                "JSON Schema sent to Grok in the tools[] parameter. "
                "Must be a valid JSON Schema object."
            ),
        }),
        ("Execution", {
            "fields": ("handler", "enabled", "is_safe"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Enabled", boolean=False)
    def enabled_badge(self, obj):
        if obj.enabled:
            return format_html(
                '<span style="background:#198754;color:#fff;padding:2px 8px;'
                'border-radius:4px;font-size:0.8em">Yes</span>'
            )
        return format_html(
            '<span style="background:#dc3545;color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:0.8em">No</span>'
        )


# ── Inline: tool calls inside a job (used by AIAnalysisJobAdmin) ──────────────

class ToolCallInline(admin.TabularInline):
    model   = ToolCall
    extra   = 0
    fields  = (
        "tool", "status", "arguments", "result",
        "error_message", "started_at", "finished_at",
    )
    readonly_fields = fields
    show_change_link = True
    can_delete = False


# ── ToolCall (standalone list) ────────────────────────────────────────────────

@admin.register(ToolCall)
class ToolCallAdmin(admin.ModelAdmin):
    list_display  = (
        "id", "tool", "job", "status_badge",
        "duration_ms", "created_at",
    )
    list_filter   = ("status", "tool__category", "created_at")
    search_fields = ("tool__name", "job__id", "error_message")
    readonly_fields = (
        "tool", "job", "arguments", "result",
        "error_message", "status",
        "started_at", "finished_at", "created_at",
    )
    ordering = ("-created_at",)

    fieldsets = (
        ("Call", {
            "fields": ("job", "tool", "status"),
        }),
        ("Payload", {
            "fields": ("arguments", "result", "error_message"),
        }),
        ("Timing", {
            "fields": ("started_at", "finished_at", "created_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending": "#6c757d",
            "running": "#0d6efd",
            "success": "#198754",
            "error":   "#dc3545",
            "skipped": "#adb5bd",
        }
        colour = colours.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:0.8em">{}</span>',
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="Duration (ms)")
    def duration_ms(self, obj):
        ms = obj.duration_ms
        return f"{ms} ms" if ms is not None else "—"