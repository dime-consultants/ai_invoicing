# tools/admin.py
from django.contrib import admin
from django.utils.html import format_html

from .models import ToolDefinition, ToolCall, UserToolConfig


def _badge(colour: str, label: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:4px;font-size:0.8em">{}</span>',
        colour,
        label,
    )


# ── UserToolConfig inline ─────────────────────────────────────────────────────

class UserToolConfigInline(admin.StackedInline):
    model       = UserToolConfig
    extra       = 0
    can_delete  = False
    fields      = (
        "webhook_url", "webhook_method", "webhook_headers", "webhook_timeout_seconds",
        "system_prompt", "output_schema",
    )
    verbose_name = "User Tool Config"


# ── ToolDefinition ────────────────────────────────────────────────────────────

@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display    = (
        "name", "display_name", "category", "tool_type_badge",
        "version", "enabled_badge", "is_safe", "created_by", "updated_at",
    )
    list_filter     = ("category", "enabled", "is_safe", "tool_type")
    search_fields   = ("name", "display_name", "description", "handler")
    ordering        = ("category", "name")
    readonly_fields = ("created_at", "updated_at")
    inlines         = [UserToolConfigInline]

    fieldsets = (
        ("Identity", {
            "fields": ("name", "display_name", "description", "category", "version"),
        }),
        ("Type & Ownership", {
            "fields": ("tool_type", "created_by"),
            "description": (
                "tool_type controls how this tool is dispatched. "
                "'builtin' uses the handler path; 'webhook' POSTs to an external URL; "
                "'prompt_transform' sends file content to Grok with a custom prompt."
            ),
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

    @admin.display(description="Enabled")
    def enabled_badge(self, obj):
        return _badge("#198754", "Yes") if obj.enabled else _badge("#dc3545", "No")

    @admin.display(description="Type")
    def tool_type_badge(self, obj):
        colours = {
            "builtin":          "#0d6efd",
            "webhook":          "#fd7e14",
            "prompt_transform": "#6f42c1",
        }
        return _badge(colours.get(obj.tool_type, "#6c757d"), obj.get_tool_type_display())

    def get_readonly_fields(self, request, obj=None):
        # Prevent editing handler path and tool_type on builtin tools
        # from the admin — only name/description/enabled are safe to touch.
        if obj and obj.tool_type == "builtin" and not request.user.is_superuser:
            return self.readonly_fields + ("handler", "tool_type", "created_by")
        return self.readonly_fields


# ── ToolCall inline ───────────────────────────────────────────────────────────

class ToolCallInline(admin.TabularInline):
    model            = ToolCall
    extra            = 0
    fields           = ("tool", "status", "arguments", "result",
                        "error_message", "started_at", "finished_at")
    readonly_fields  = fields
    show_change_link = True
    can_delete       = False


# ── ToolCall ──────────────────────────────────────────────────────────────────

@admin.register(ToolCall)
class ToolCallAdmin(admin.ModelAdmin):
    list_display    = ("id", "tool", "job", "status_badge", "duration_ms_display", "created_at")
    list_filter     = ("status", "tool__category", "tool__tool_type", "created_at")
    search_fields   = ("tool__name", "error_message")
    readonly_fields = ("tool", "job", "arguments", "result",
                       "error_message", "status",
                       "started_at", "finished_at", "created_at")
    ordering        = ("-created_at",)

    fieldsets = (
        ("Call",    {"fields": ("job", "tool", "status")}),
        ("Payload", {"fields": ("arguments", "result", "error_message")}),
        ("Timing",  {"fields": ("started_at", "finished_at", "created_at"),
                     "classes": ("collapse",)}),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending": "#6c757d", "running": "#0d6efd",
            "success": "#198754", "error":   "#dc3545", "skipped": "#adb5bd",
        }
        return _badge(colours.get(obj.status, "#6c757d"), obj.get_status_display())

    @admin.display(description="Duration (ms)")
    def duration_ms_display(self, obj):
        ms = obj.duration_ms
        return f"{ms} ms" if ms is not None else "—"


# ── UserToolConfig ────────────────────────────────────────────────────────────

@admin.register(UserToolConfig)
class UserToolConfigAdmin(admin.ModelAdmin):
    list_display    = ("tool", "webhook_url", "webhook_method",
                       "webhook_timeout_seconds", "updated_at")
    search_fields   = ("tool__name", "webhook_url")
    readonly_fields = ("created_at", "updated_at")
    ordering        = ("-created_at",)

    fieldsets = (
        ("Tool", {
            "fields": ("tool",),
        }),
        ("Webhook Config", {
            "fields": (
                "webhook_url", "webhook_method",
                "webhook_headers", "webhook_timeout_seconds",
            ),
            "description": "Only relevant when tool_type = 'webhook'.",
        }),
        ("Prompt Transform Config", {
            "fields": ("system_prompt", "output_schema"),
            "description": (
                "Only relevant when tool_type = 'prompt_transform'. "
                "Use {file_text} in system_prompt as the placeholder for uploaded file content. "
                "Use {arguments} for any other tool arguments."
            ),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )