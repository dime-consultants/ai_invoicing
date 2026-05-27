# uploads/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import UploadBatch, UploadedFile


def _badge(colour: str, label: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:4px;font-size:0.8em">{}</span>',
        colour, label,
    )


class UploadedFileInline(admin.TabularInline):
    model            = UploadedFile
    extra            = 0
    fields           = ("original_filename", "extension", "file_size_bytes",
                        "detected_type", "detection_confidence", "parse_status")
    readonly_fields  = fields
    show_change_link = True
    can_delete       = False


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display    = ("id", "label", "uploaded_by", "status_badge",
                       "file_count", "processed_count", "error_count", "created_at")
    list_filter     = ("status", "created_at")
    search_fields   = ("label", "description", "uploaded_by__username")
    readonly_fields = ("file_count", "processed_count", "error_count",
                       "created_at", "updated_at")
    ordering        = ("-created_at",)
    inlines         = [UploadedFileInline]

    fieldsets = (
        (None, {"fields": ("label", "description", "uploaded_by", "status")}),
        ("Counts", {"fields": ("file_count", "processed_count", "error_count"),
                    "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"),
                        "classes": ("collapse",)}),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending":    "#6c757d", "processing": "#0d6efd",
            "completed":  "#198754", "partial":    "#fd7e14", "failed": "#dc3545",
        }
        return _badge(colours.get(obj.status, "#6c757d"), obj.get_status_display())


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display    = ("original_filename", "extension", "batch",
                       "detected_type", "detection_confidence",
                       "parse_status_badge", "file_size_bytes", "uploaded_at")
    list_filter     = ("parse_status", "extension", "detected_type")
    search_fields   = ("original_filename", "detected_type", "batch__label")
    readonly_fields = ("file_size_bytes", "mime_type", "extension",
                       "detected_type", "detection_confidence",
                       "parse_status", "parse_error", "parsed_at",
                       "extracted_text", "uploaded_at")
    ordering        = ("-uploaded_at",)

    fieldsets = (
        ("File", {"fields": ("batch", "file", "original_filename",
                             "file_size_bytes", "mime_type", "extension")}),
        ("AI Detection", {"fields": ("detected_type", "detection_confidence")}),
        ("Parsing", {"fields": ("parse_status", "parse_error", "parsed_at")}),
        ("Extracted Content", {"fields": ("extracted_text",),
                               "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("uploaded_at",), "classes": ("collapse",)}),
    )

    @admin.display(description="Parse Status")
    def parse_status_badge(self, obj):
        colours = {
            "pending":     "#6c757d", "parsing":     "#0d6efd",
            "parsed":      "#198754", "parse_error": "#dc3545", "skipped": "#adb5bd",
        }
        return _badge(colours.get(obj.parse_status, "#6c757d"), obj.get_parse_status_display())