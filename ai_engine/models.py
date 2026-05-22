# ai_engine/models.py
from django.db import models


class AIAnalysisJob(models.Model):
    """
    One AI processing session against one or more uploaded files.

    Lifecycle
    ---------
    queued → running → done | error

    The job holds the full conversation with Grok: the system prompt,
    the user intent, every tool call made during the session, and the
    final response.  Token counts are stored for cost tracking.

    A job always belongs to a batch.  It may reference a single
    UploadedFile (fine-grained) or the whole batch (bulk analysis).
    """

    TASK_TYPE_CHOICES = [
        # ── File processing ───────────────────────────────────────────
        ("detect_and_extract", "Detect & Extract"),    # AI reads file, chooses tool, extracts data
        ("reconcile",          "Reconcile"),           # cross-check two datasets
        # ── Analysis ─────────────────────────────────────────────────
        ("summarise_batch",    "Summarise Batch"),
        ("flag_anomalies",     "Flag Anomalies"),
        ("explain_variance",   "Explain Variance"),
        ("classify_lines",     "Classify Line Items"),
        # ── Report ───────────────────────────────────────────────────
        ("generate_report",    "Generate Report"),
        # ── Free-form ────────────────────────────────────────────────
        ("custom",             "Custom Prompt"),
    ]

    STATUS_CHOICES = [
        ("queued",   "Queued"),
        ("running",  "Running"),
        ("done",     "Done"),
        ("error",    "Error"),
    ]

    # ── Relationships ─────────────────────────────────────────────────────────
    batch = models.ForeignKey(
        "uploads.UploadBatch",
        on_delete=models.CASCADE,
        related_name="ai_jobs",
    )
    # Optional: target a specific file rather than the whole batch
    target_file = models.ForeignKey(
        "uploads.UploadedFile",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ai_jobs",
        help_text="If set, this job focuses on one file; otherwise the whole batch.",
    )
    requested_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ai_jobs",
    )

    # ── Task ──────────────────────────────────────────────────────────────────
    task_type   = models.CharField(max_length=30, choices=TASK_TYPE_CHOICES)
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default="queued")

    # Free-form intent from the user — fed into the system / user prompt
    user_intent = models.TextField(
        blank=True,
        help_text=(
            "Natural-language description of what the user wants, "
            "e.g. 'Extract all receipts and flag any with taxes above 10%'."
        ),
    )

    # ── Prompts & Response ────────────────────────────────────────────────────
    system_prompt    = models.TextField(blank=True)
    user_prompt      = models.TextField(
        help_text="The assembled prompt sent to the LLM (may include extracted file text).",
    )
    raw_response     = models.TextField(blank=True)
    structured_output = models.JSONField(
        null=True, blank=True,
        help_text="Final parsed result — anomalies list, summary dict, report metadata, etc.",
    )

    # ── Token / cost tracking ─────────────────────────────────────────────────
    input_tokens  = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)

    # ── Error ─────────────────────────────────────────────────────────────────
    error_message = models.TextField(blank=True)

    # ── Timing ───────────────────────────────────────────────────────────────
    started_at  = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "AI Analysis Job"
        verbose_name_plural = "AI Analysis Jobs"
        ordering            = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"{self.get_task_type_display()} | "
            f"{self.status} | "
            f"Batch #{self.batch_id}"
        )

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AIInsight(models.Model):
    """
    A single structured finding produced by an AIAnalysisJob.

    Could be an anomaly flag, a variance explanation, a summary bullet,
    or a classification result.  Insights are the human-readable output
    of the AI pipeline — they appear in the UI and can be actioned by
    finance users.
    """

    INSIGHT_TYPE_CHOICES = [
        ("anomaly",              "Anomaly"),
        ("variance_explanation", "Variance Explanation"),
        ("summary_point",        "Summary Point"),
        ("classification",       "Classification"),
        ("recommendation",       "Recommendation"),
    ]

    SEVERITY_CHOICES = [
        ("info",     "Info"),
        ("warning",  "Warning"),
        ("critical", "Critical"),
    ]

    job = models.ForeignKey(
        AIAnalysisJob,
        on_delete=models.CASCADE,
        related_name="insights",
    )

    insight_type = models.CharField(max_length=30, choices=INSIGHT_TYPE_CHOICES)
    severity     = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default="info")

    # Ties this insight back to a specific invoice / line item
    reference_key = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "The CU invoice number, ACON item number, or phone number "
            "this insight relates to."
        ),
    )

    title  = models.CharField(max_length=255)
    detail = models.TextField()

    # Which tool call surfaced this insight (optional traceability)
    source_tool_call = models.ForeignKey(
        "tools.ToolCall",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="insights",
        help_text="The tool call that produced this insight, if applicable.",
    )

    # Actioning
    is_actioned = models.BooleanField(default=False)
    actioned_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="actioned_insights",
    )
    actioned_at    = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "AI Insight"
        verbose_name_plural = "AI Insights"
        ordering            = ["-severity", "created_at"]

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.title}"