# uploads/admin.py
from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import UploadBatch, UploadedFile


def _badge(colour: str, label: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:4px;font-size:0.8em">{}</span>',
        colour, label,
    )


class UploadedFileInline(admin.TabularInline):
    model = UploadedFile
    extra = 0
    fields = (
        "original_filename",
        "extension",
        "file_size_bytes",
        "page_count",
        "detected_type",
        "detection_confidence",
        "parse_status",
        "extraction_deferred",
    )
    readonly_fields = fields
    show_change_link = True
    can_delete = False


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label",
        "uploaded_by",
        "status_badge",
        "file_count",
        "processed_count",
        "error_count",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("label", "description", "uploaded_by__username", "uploaded_by__email")
    readonly_fields = (
        "file_count",
        "processed_count",
        "error_count",
        "created_at",
        "updated_at",
    )
    ordering = ("-created_at",)
    inlines = [UploadedFileInline]
    date_hierarchy = "created_at"

    fieldsets = (
        (None, {"fields": ("label", "description", "uploaded_by", "status")}),
        (
            "Counts",
            {
                "fields": ("file_count", "processed_count", "error_count"),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    actions = ["refresh_counters"]

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending": "#6c757d",
            "processing": "#0d6efd",
            "completed": "#198754",
            "partial": "#fd7e14",
            "failed": "#dc3545",
        }
        return _badge(colours.get(obj.status, "#6c757d"), obj.get_status_display())

    @admin.action(description="Refresh batch counters from files")
    def refresh_counters(self, request, queryset):
        from .services import UploadService

        updated = 0
        for batch in queryset:
            UploadService._refresh_batch_counters(batch)
            updated += 1
        self.message_user(
            request,
            f"Refreshed counters for {updated} batch(es).",
            messages.SUCCESS,
        )


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename",
        "extension",
        "batch_link",
        "page_count_display",
        "detected_type",
        "detection_confidence",
        "parse_status_badge",
        "deferred_badge",
        "file_size_display",
        "uploaded_at",
    )
    list_filter = (
        "parse_status",
        "extraction_deferred",
        "extension",
        "detected_type",
        "uploaded_at",
    )
    search_fields = (
        "original_filename",
        "detected_type",
        "batch__label",
        "parse_error",
    )
    readonly_fields = (
        "file_size_bytes",
        "mime_type",
        "extension",
        "page_count",
        "extraction_deferred",
        "detected_type",
        "detection_confidence",
        "parse_status",
        "parse_error",
        "parsed_at",
        "extracted_text_preview",
        "uploaded_at",
        "file_link",
    )
    ordering = ("-uploaded_at",)
    date_hierarchy = "uploaded_at"
    list_select_related = ("batch",)
    raw_id_fields = ("batch",)

    fieldsets = (
        (
            "File",
            {
                "fields": (
                    "batch",
                    "file",
                    "file_link",
                    "original_filename",
                    "file_size_bytes",
                    "mime_type",
                    "extension",
                    "page_count",
                )
            },
        ),
        (
            "AI Detection",
            {"fields": ("detected_type", "detection_confidence")},
        ),
        (
            "Parsing",
            {
                "fields": (
                    "parse_status",
                    "extraction_deferred",
                    "parse_error",
                    "parsed_at",
                )
            },
        ),
        (
            "Extracted Content (preview)",
            {
                "fields": ("extracted_text_preview",),
                "classes": ("collapse",),
                "description": (
                    "Truncated preview stored in DB. Full content for large PDFs "
                    "is obtained via the read_file tool with page_from / page_to."
                ),
            },
        ),
        (
            "Timestamps",
            {"fields": ("uploaded_at",), "classes": ("collapse",)},
        ),
    )

    actions = [
        "queue_reextract",
        "mark_skipped",
    ]

    # ── List display helpers ──────────────────────────────────────────────────

    @admin.display(description="Batch", ordering="batch__id")
    def batch_link(self, obj):
        if not obj.batch_id:
            return "—"
        return format_html(
            '<a href="/admin/uploads/uploadbatch/{}/change/">#{} — {}</a>',
            obj.batch_id,
            obj.batch_id,
            (obj.batch.label or "")[:40],
        )

    @admin.display(description="Pages", ordering="page_count")
    def page_count_display(self, obj):
        if obj.page_count is None:
            return "—"
        if obj.is_large_pdf:
            return format_html(
                '<strong style="color:#fd7e14">{}</strong> <span style="color:#888">(large)</span>',
                obj.page_count,
            )
        return str(obj.page_count)

    @admin.display(description="Size")
    def file_size_display(self, obj):
        size = obj.file_size_bytes or 0
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    @admin.display(description="Parse Status")
    def parse_status_badge(self, obj):
        colours = {
            "pending": "#6c757d",
            "parsing": "#0d6efd",
            "parsed": "#198754",
            "parse_error": "#dc3545",
            "skipped": "#adb5bd",
        }
        return _badge(
            colours.get(obj.parse_status, "#6c757d"),
            obj.get_parse_status_display(),
        )

    @admin.display(description="Deferred")
    def deferred_badge(self, obj):
        if obj.extraction_deferred:
            return _badge("#fd7e14", "async")
        return mark_safe('<span style="color:#adb5bd">—</span>')

    @admin.display(description="File")
    def file_link(self, obj):
        if not obj.file:
            return "—"
        try:
            url = obj.file.url
            return format_html('<a href="{}" target="_blank">Download</a>', url)
        except Exception:
            return "—"

    @admin.display(description="Extracted text (first 2 000 chars)")
    def extracted_text_preview(self, obj):
        text = (obj.extracted_text or "").strip()
        if not text:
            return mark_safe('<em style="color:#888">No extracted text yet.</em>')
        preview = text[:2000]
        if len(text) > 2000:
            preview += f"\n\n… [{len(text) - 2000} more characters truncated in preview]"
        return format_html(
            '<pre style="white-space:pre-wrap;max-height:320px;overflow:auto;'
            'background:#f8f9fa;padding:8px;border-radius:4px;font-size:0.85em">{}</pre>',
            preview,
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    @admin.action(description="Queue re-extraction (Celery)")
    def queue_reextract(self, request, queryset):
        try:
            from .tasks import reextract_file_task
        except ImportError:
            self.message_user(
                request,
                "uploads.tasks.reextract_file_task is not available.",
                messages.ERROR,
            )
            return

        queued = 0
        for uf in queryset:
            reextract_file_task.delay(uf.pk)
            queued += 1
        self.message_user(
            request,
            f"Queued re-extraction for {queued} file(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Mark as skipped")
    def mark_skipped(self, request, queryset):
        updated = queryset.update(parse_status="skipped", parse_error="")
        # Refresh parent batch counters
        from .services import UploadService

        batch_ids = set(queryset.values_list("batch_id", flat=True))
        for bid in batch_ids:
            if bid:
                try:
                    batch = UploadBatch.objects.get(pk=bid)
                    UploadService._refresh_batch_counters(batch)
                except UploadBatch.DoesNotExist:
                    pass
        self.message_user(
            request,
            f"Marked {updated} file(s) as skipped.",
            messages.SUCCESS,
        )