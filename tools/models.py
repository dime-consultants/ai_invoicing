# tools/models.py
import uuid

from django.db import models
from django.utils import timezone


class ToolDefinition(models.Model):
    """
    Registry of every tool the AI can call.

    Each row describes one tool in the exact shape Grok expects in its
    `tools` parameter — name, description, and a JSON Schema for the
    input parameters.  The `handler` field tells the service layer which
    Python function to dispatch to when the AI calls this tool.

    Tools are versioned so we can roll out updated schemas without
    breaking jobs that reference older definitions.
    """

    CATEGORY_CHOICES = [
        ("extraction",     "Data Extraction"),    # parse raw files → structured rows
        ("transformation", "Transformation"),     # clean / normalise / reshape data
        ("reconciliation", "Reconciliation"),     # cross-check two data sets
        ("analysis",       "AI Analysis"),        # summarise / flag / explain
        ("report",         "Report Generation"),  # produce downloadable output files
        ("utility",        "Utility"),            # helpers (date parse, fx lookup, etc.)
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    name = models.CharField(
        max_length=80,
        unique=True,
        help_text="Snake_case name exactly as sent to the LLM, e.g. 'extract_ura_receipts'.",
    )
    display_name = models.CharField(max_length=120)
    description  = models.TextField(
        help_text=(
            "Plain-English description the LLM reads to decide whether to call this tool. "
            "Be specific about what file types it accepts and what it returns."
        ),
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="utility")

    # JSON Schema object for the tool's input parameters.
    # Sent verbatim in the `tools[].function.parameters` field.
    parameters_schema = models.JSONField(
        default=dict,
        help_text=(
            'JSON Schema describing the tool\'s input parameters. '
            'Example: {"type":"object","properties":{"file_id":{"type":"integer"}},'
            '"required":["file_id"]}'
        ),
    )

    # Dotted Python path to the function that executes this tool.
    # e.g. "tools.handlers.extract_ura_receipts"
    handler = models.CharField(
        max_length=200,
        help_text="Dotted import path to the Python function that runs this tool.",
    )

    version  = models.PositiveSmallIntegerField(default=1)
    enabled  = models.BooleanField(default=True)
    is_safe  = models.BooleanField(
        default=True,
        help_text=(
            "If False, requires explicit user confirmation before execution "
            "(e.g. tools that write to external systems)."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    TOOL_TYPE_CHOICES = [
    ("builtin",          "Built-in Handler"),
    ("webhook",          "Webhook"),
    ("prompt_transform", "Prompt Transform"),
    ]
    tool_type = models.CharField(
        max_length=20,
        choices=TOOL_TYPE_CHOICES,
        default="builtin",
    )
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="defined_tools",
        help_text="Null = system built-in; set = user-defined.",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        help_text="Null for system built-ins. Set for user-defined tools — shared across the org.",
    )


    class Meta:
        verbose_name        = "Tool Definition"
        verbose_name_plural = "Tool Definitions"
        ordering            = ["category", "name"]

    def __str__(self) -> str:
        return f"{self.name} v{self.version} ({self.get_category_display()})"

    def to_grok_schema(self) -> dict:
        """
        Return this tool in the shape Grok expects inside the `tools` list:

        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": { <JSON Schema> }
            }
        }
        """
        return {
            "type": "function",
            "function": {
                "name":        self.name,
                "description": self.description,
                "parameters":  self.parameters_schema,
            },
        }


class ToolCall(models.Model):
    """
    A single invocation of a ToolDefinition by the AI during a job.

    Stores the exact arguments the LLM passed, the result returned,
    timing, and whether the call succeeded.  This gives a full audit
    trail of every action the AI took.
    """

    STATUS_CHOICES = [
        ("pending",  "Pending"),   # queued, not yet executed
        ("running",  "Running"),
        ("success",  "Success"),
        ("error",    "Error"),
        ("skipped",  "Skipped"),   # tool disabled or unsafe — not executed
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    # The job that triggered this call (nullable so tools can be called standalone)
    job = models.ForeignKey(
        "ai_engine.AIAnalysisJob",
        on_delete=models.CASCADE,
        related_name="tool_calls",
        null=True,
        blank=True,
    )
    tool = models.ForeignKey(
        ToolDefinition,
        on_delete=models.PROTECT,
        related_name="calls",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        help_text="Denormalized at creation time — job may be null (standalone/test calls).",
    )

    # Exact arguments the LLM passed (from tool_call.function.arguments)
    arguments = models.JSONField(
        default=dict,
        help_text="Arguments as parsed from the LLM's tool_call response.",
    )

    # What the handler returned — fed back to the LLM as the tool_result
    result = models.JSONField(
        null=True,
        blank=True,
        help_text="Serialisable return value from the handler function.",
    )
    error_message = models.TextField(blank=True)

    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Tool Call"
        verbose_name_plural = "Tool Calls"
        ordering            = ["created_at"]

    def __str__(self) -> str:
        return f"ToolCall({self.tool.name}) [{self.status}]"

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            delta = self.finished_at - self.started_at
            return int(delta.total_seconds() * 1000)
        return None


class UserToolConfig(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    tool = models.OneToOneField(
        ToolDefinition,
        on_delete=models.CASCADE,
        related_name="user_config",
    )

    # ── webhook ──────────────────────────────────────────────────────
    webhook_url     = models.URLField(blank=True)
    webhook_method  = models.CharField(
        max_length=10,
        choices=[("POST", "POST"), ("GET", "GET")],
        default="POST",
    )
    webhook_headers = models.JSONField(
        default=dict,
        help_text="Extra headers to send with the webhook request (e.g. auth tokens).",
    )
    webhook_timeout_seconds = models.PositiveSmallIntegerField(default=30)

    # ── prompt_transform ─────────────────────────────────────────────
    system_prompt   = models.TextField(
        blank=True,
        help_text=(
            "System prompt for prompt_transform tools. "
            "Use {file_text} as the placeholder for the uploaded file's content."
        ),
    )
    output_schema   = models.JSONField(
        null=True, blank=True,
        help_text="Optional JSON Schema the LLM output should conform to.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
