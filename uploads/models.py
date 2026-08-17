import os
from django.db import models
from django.utils import timezone


def _batch_upload_path(instance, filename):
    """Store files under  media/uploads/<year>/<month>/<batch_id>/<filename>"""
    now = timezone.now()
    return os.path.join(
        "uploads",
        str(now.year),
        f"{now.month:02d}",
        str(instance.batch.id) if instance.batch_id else "unassigned",
        filename,
    )


class UploadBatch(models.Model):
    """
    A logical grouping of one or more uploaded files submitted together
    for a single processing run.

    The 'label' is a free-form human name (e.g. "April 2026 URA receipts").
    Source type is intentionally left open — the AI will inspect the files
    and decide how to process them.
    """

    STATUS_CHOICES = [
        ("pending",    "Pending"),       # files uploaded, no processing started
        ("processing", "Processing"),    # AI jobs / extraction running
        ("completed",  "Completed"),     # all jobs finished successfully
        ("partial",    "Partial"),       # some jobs failed
        ("failed",     "Failed"),        # all jobs failed
    ]

    label = models.CharField(
        max_length=200,
        help_text="Human-readable name for this batch, e.g. 'April 2026 URA Receipts'.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional notes about what this batch contains or why it was uploaded.",
    )
    status = models.CharField(
        max_length=15,
        choices=STATUS_CHOICES,
        default="pending",
    )
    uploaded_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="batches",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )

    # Denormalised counters — updated by signals / service layer
    file_count      = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    error_count     = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Upload Batch"
        verbose_name_plural = "Upload Batches"
        ordering            = ["-created_at"]

    def __str__(self) -> str:
        return f"Batch #{self.pk} — {self.label} [{self.status}]"

    @property
    def is_complete(self) -> bool:
        return self.status == "completed"


class UploadedFile(models.Model):
    """
    A single file belonging to an UploadBatch.

    parse_status tracks where this specific file is in the pipeline.
    detected_type is populated by the AI after it inspects the file content,
    e.g. 'ura_fiscal_receipt', 'safaricom_bill', 'acon_export', 'unknown'.

    For large PDFs (page_count > ASYNC_PAGE_THRESHOLD):
      - ingest_file sets parse_status="pending" and queues extract_file_text_task
      - the Celery task extracts page-by-page and moves status to "parsed" / "parse_error"
      - the agent uses read_file(file_id, page_from, page_to) for chunked access
    """

    PARSE_STATUS_CHOICES = [
        ("pending",     "Pending"),      # queued for (or waiting on) extraction
        ("parsing",     "Parsing"),      # Celery worker currently extracting
        ("parsed",      "Parsed"),       # extracted_text ready (or page-range ready)
        ("parse_error", "Parse Error"),
        ("skipped",     "Skipped"),
    ]

    batch = models.ForeignKey(
        UploadBatch,
        on_delete=models.CASCADE,
        related_name="files",
    )
    file = models.FileField(upload_to=_batch_upload_path)
    original_filename = models.CharField(max_length=255)
    file_size_bytes   = models.PositiveBigIntegerField(default=0)
    mime_type         = models.CharField(max_length=100, blank=True)
    extension         = models.CharField(
        max_length=20,
        blank=True,
        help_text="Lowercased extension without dot, e.g. 'txt', 'pdf', 'xlsx'.",
    )

    # Set by the AI after inspecting file content
    detected_type = models.CharField(
        max_length=60,
        blank=True,
        help_text=(
            "AI-assigned document type, e.g. 'ura_fiscal_receipt', "
            "'safaricom_bill', 'acon_export', 'unknown'."
        ),
    )
    detection_confidence = models.CharField(
        max_length=10,
        blank=True,
        help_text="'high', 'medium', or 'low'.",
    )

    parse_status  = models.CharField(
        max_length=15,
        choices=PARSE_STATUS_CHOICES,
        default="pending",
    )
    parse_error   = models.TextField(blank=True)
    parsed_at     = models.DateTimeField(null=True, blank=True)

    # Raw text extracted from the file (stored for AI context window injection).
    # Always the FULL extracted text, regardless of document size — ingestion
    # never truncates. Paginated access for a single tool call is available
    # via read_file(file_id, page_from=..., page_to=...).
    extracted_text = models.TextField(
        blank=True,
        help_text="Plain-text representation of the file content used by the AI.",
    )
    page_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Number of pages in the document, if applicable.",
    )

    # True when extraction was deferred to Celery (large PDF path)
    extraction_deferred = models.BooleanField(
        default=False,
        help_text="True if text extraction was queued to a background worker.",
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Uploaded File"
        verbose_name_plural = "Uploaded Files"
        ordering            = ["batch", "uploaded_at"]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.extension}) — {self.parse_status}"

    @property
    def filename(self) -> str:
        return self.original_filename

    @property
    def is_parsed(self) -> bool:
        return self.parse_status == "parsed"

    @property
    def is_large_pdf(self) -> bool:
        from django.conf import settings
        threshold = getattr(settings, "ASYNC_PAGE_THRESHOLD", 20)
        return (
            self.extension == "pdf"
            and self.page_count is not None
            and self.page_count > threshold
        )