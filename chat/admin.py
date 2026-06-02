# chat/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import ChatConversation, ChatMessage, ChatMessageAttachment, Workflow


def _badge(colour: str, label: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:4px;font-size:0.8em">{}</span>',
        colour, label,
    )


class ChatMessageInline(admin.TabularInline):
    model            = ChatMessage
    extra            = 0
    fields           = ("role", "content_preview", "ai_job", "applied_workflow", "created_at")
    readonly_fields  = fields
    show_change_link = True
    can_delete       = False
    ordering         = ("created_at",)

    @admin.display(description="Content")
    def content_preview(self, obj):
        return obj.content[:100] + "…" if len(obj.content) > 100 else obj.content


class ChatMessageAttachmentInline(admin.TabularInline):
    model            = ChatMessageAttachment
    extra            = 0
    fields           = ("filename", "file_type", "attachment_type",
                        "file_size_bytes", "uploaded_file", "created_at")
    readonly_fields  = fields
    show_change_link = True
    can_delete       = False


@admin.register(ChatConversation)
class ChatConversationAdmin(admin.ModelAdmin):
    list_display    = ("id", "user", "title", "related_batch",
                       "is_active", "message_count", "updated_at")
    list_filter     = ("is_active", "created_at")
    search_fields   = ("title", "user__username", "user__email")
    readonly_fields = ("created_at", "updated_at")
    ordering        = ("-updated_at",)
    inlines         = [ChatMessageInline]

    fieldsets = (
        (None, {"fields": ("user", "title", "related_batch", "is_active")}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Messages")
    def message_count(self, obj):
        return obj.messages.count()


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display    = ("id", "role_badge", "conversation",
                       "content_preview", "ai_job", "created_at")
    list_filter     = ("role", "created_at")
    search_fields   = ("content", "conversation__title", "conversation__user__username")
    readonly_fields = ("created_at",)
    ordering        = ("-created_at",)
    inlines         = [ChatMessageAttachmentInline]

    fieldsets = (
        (None,    {"fields": ("conversation", "role", "content")}),
        ("Links", {"fields": ("ai_job", "applied_workflow")}),
        ("Timestamps", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    @admin.display(description="Role")
    def role_badge(self, obj):
        colours = {"user": "#0d6efd", "assistant": "#198754", "system": "#6c757d"}
        return _badge(colours.get(obj.role, "#6c757d"), obj.get_role_display())

    @admin.display(description="Content")
    def content_preview(self, obj):
        return obj.content[:80] + "…" if len(obj.content) > 80 else obj.content


@admin.register(ChatMessageAttachment)
class ChatMessageAttachmentAdmin(admin.ModelAdmin):
    list_display    = ("filename", "file_type", "attachment_type_badge",
                       "message", "file_size_bytes", "created_at")
    list_filter     = ("attachment_type", "file_type", "created_at")
    search_fields   = ("filename", "message__conversation__title")
    readonly_fields = ("message", "file", "filename", "file_type",
                       "attachment_type", "file_size_bytes", "uploaded_file", "created_at")
    ordering        = ("-created_at",)

    @admin.display(description="Type")
    def attachment_type_badge(self, obj):
        colours = {"user_upload": "#0d6efd", "assistant_output": "#198754"}
        return _badge(colours.get(obj.attachment_type, "#6c757d"),
                      obj.get_attachment_type_display())


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display    = ("name", "workflow_type", "enabled_badge",
                       "is_default", "step_count", "updated_at")
    list_filter     = ("workflow_type", "enabled", "is_default")
    search_fields   = ("name", "description")
    readonly_fields = ("created_at", "updated_at")
    ordering        = ("workflow_type", "name")

    fieldsets = (
        (None, {"fields": ("name", "description", "workflow_type", "enabled", "is_default")}),
        ("Tool Steps", {"fields": ("steps",)}),
        ("Prompt Override", {"fields": ("system_prompt_prefix",), "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Enabled")
    def enabled_badge(self, obj):
        return _badge("#198754", "Yes") if obj.enabled else _badge("#dc3545", "No")

    @admin.display(description="Steps")
    def step_count(self, obj):
        n = len(obj.steps or [])
        return f"{n} step{'s' if n != 1 else ''}"