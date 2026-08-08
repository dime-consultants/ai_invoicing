# ai_engine/services.py
"""
AIEngineService — job lifecycle + chat entry point for AI-driven file processing.

Architecture
------------
All actual tool-calling (resolving ToolDefinition rows, dispatching to
tools.handlers.*, recording ToolCall rows) lives in ONE place:
tools.services.ToolService.run(). This module does not reimplement that
loop — it only:

  1. Builds the system prompt / user message for a given task.
  2. Creates and updates AIAnalysisJob rows (queued -> running -> done/error).
  3. Calls ToolService.run(..., job=job) so every ToolCall is linked to a job.
  4. Persists AIInsight rows from the tool results once the run finishes.

Job lifecycle
-------------
queued → running → done | error
(ai_engine/signals.py listens for these transitions and pushes WebSocket
notifications.)

Entry points
------------
AIEngineService.create_job(...)          — persist a queued job
AIEngineService.dispatch(job_id)         — run it synchronously (called by
                                            ai_engine/tasks.py's Celery task)
AIEngineService.handle_chat_message(...) — called by chat / the WebSocket consumer
AIEngineService.requeue(job_id)          — reset a failed/done job and re-run it

Tool names
----------
This module references tool names from the DB (seeded by seed_tools management
command). The workflow tool-name map below uses only the abstract tool names —
no handler-level implementation details belong here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as tz

from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Grok client (lazy singleton)
# ─────────────────────────────────────────────────────────────────────────────

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        api_key = getattr(settings, "XAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is not set in settings / .env")
        _client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    return _client


GROK_MODEL = lambda: getattr(settings, "GROK_MODEL", "grok-3")


# ─────────────────────────────────────────────────────────────────────────────
# Tool name whitelists per task_type
#
# These reference only the abstract tool names registered by seed_tools.
# The dispatch layer (tools/services.py ToolService) resolves each name to
# its tool_type and runs it appropriately (builtin / webhook / prompt_transform).
#
# Builtin primitives available to all workflows:
#   read_file, detect_file_type, write_xlsx, call_webhook, run_python
#
# Domain prompt_transform tools:
#   extract_invoice_data, flag_anomalies, reconcile_datasets,
#   summarise_batch, clean_dataset
#
# Users can create additional prompt_transform or webhook tools and add
# them to a workflow by passing tool_names explicitly to ToolService.run().
# ─────────────────────────────────────────────────────────────────────────────

# Every tool available in the system — used when no workflow narrows the list
ALL_TOOL_NAMES: list[str] = [
    # primitives
    "read_file",
    "detect_file_type",
    "write_xlsx",
    "call_webhook",
    "run_python",
    # domain prompt_transform
    "extract_invoice_data",
    "flag_anomalies",
    "reconcile_datasets",
    "summarise_batch",
    "clean_dataset",
]

# Workflow-scoped tool sets — keeps Grok's context window small and
# reduces the chance of it calling irrelevant tools.
WORKFLOW_TOOL_NAMES: dict[str, list[str]] = {
    # Extract structured data from any invoice/fiscal file
    "extraction": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "write_xlsx",
    ],
    # Full reconciliation: extract both sides, compare, write report
    "reconciliation": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "reconcile_datasets",
        "write_xlsx",
    ],
    # Anomaly detection: read, extract, flag
    "anomaly_detection": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "flag_anomalies",
        "write_xlsx",
    ],
    # Data cleaning: read, clean, optionally write
    "data_cleaning": [
        "detect_file_type",
        "read_file",
        "clean_dataset",
        "write_xlsx",
    ],
    # Batch overview: summarise all files in a batch
    "batch_summary": [
        "detect_file_type",
        "read_file",
        "summarise_batch",
    ],
    # Report generation: extract + write formatted xlsx
    "report_generation": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "flag_anomalies",
        "write_xlsx",
    ],
    # Free-form: all tools available (no restriction)
    "custom": ALL_TOOL_NAMES,
}

# Legacy aliases — keeps existing job records valid if task_type was
# set using the old names before this refactor.
WORKFLOW_TOOL_NAMES["ura_processing"]       = WORKFLOW_TOOL_NAMES["extraction"]
WORKFLOW_TOOL_NAMES["safaricom_processing"] = WORKFLOW_TOOL_NAMES["extraction"]
WORKFLOW_TOOL_NAMES["acon_processing"]      = WORKFLOW_TOOL_NAMES["data_cleaning"]
WORKFLOW_TOOL_NAMES["classification"]       = WORKFLOW_TOOL_NAMES["anomaly_detection"]
WORKFLOW_TOOL_NAMES["summarise_batch"]      = WORKFLOW_TOOL_NAMES["batch_summary"]
WORKFLOW_TOOL_NAMES["flag_anomalies"]       = WORKFLOW_TOOL_NAMES["anomaly_detection"]
WORKFLOW_TOOL_NAMES["reconcile"]            = WORKFLOW_TOOL_NAMES["reconciliation"]


# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """\
You are an AI assistant for a finance team. You help process, extract,
reconcile, and audit invoice and receipt files using the tools available to you.

When a user gives you a file or batch:
1. Call detect_file_type first to identify the document type.
2. Call read_file to get the file content.
3. Choose the right domain tool (extract_invoice_data, reconcile_datasets,
   flag_anomalies, clean_dataset, or summarise_batch) based on what the user wants.
4. If the user wants a downloadable file, call write_xlsx with the structured output.
5. Summarise your findings in plain English after all tool calls complete.

Rules:
- Quote specific numbers from tool results — never invent data.
- If a tool returns ok=false, explain the error and suggest a fix.
- Be concise. Finance users are busy."""


def _build_system_prompt(workflow=None, user_intent: str = "") -> str:
    prompt = BASE_SYSTEM_PROMPT
    if workflow and getattr(workflow, "system_prompt_prefix", ""):
        prompt = workflow.system_prompt_prefix.strip() + "\n\n" + prompt
    if user_intent:
        prompt += f"\n\nUser's specific request: {user_intent}"
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# Insight extraction
# ─────────────────────────────────────────────────────────────────────────────

def _persist_insights(job, tool_results: list[dict]) -> None:
    """
    Walk tool results and write AIInsight rows for anything flag_anomalies
    or reconcile_datasets produced.
    """
    from .models import AIInsight

    for result in tool_results:
        if not isinstance(result, dict) or not result.get("ok"):
            continue

        # flag_anomalies result
        for anomaly in result.get("anomalies", []):
            AIInsight.objects.create(
                job=job,
                insight_type="anomaly",
                severity=anomaly.get("severity", "info"),
                reference_key=str(anomaly.get("id", ""))[:100],
                title=anomaly.get("anomaly_type", "Anomaly").replace("_", " ").title(),
                detail=anomaly.get("detail", ""),
            )

        # reconcile_datasets result
        for row in result.get("rows", []):
            if row.get("status") in ("VARIANCE", "MISSING_IN_B", "MISSING_IN_A"):
                severity = (
                    "critical" if "MISSING" in row.get("status", "")
                    else "warning"
                )
                AIInsight.objects.create(
                    job=job,
                    insight_type="variance_explanation",
                    severity=severity,
                    reference_key=str(row.get("id", ""))[:100],
                    title=row.get("status", "Variance"),
                    detail=(
                        f"Amount A: {row.get('amount_a')} | "
                        f"Amount B: {row.get('amount_b')} | "
                        f"Variance: {row.get('variance')}"
                    ),
                )

        # summarise_batch result
        if result.get("narrative"):
            AIInsight.objects.create(
                job=job,
                insight_type="summary_point",
                severity="info",
                title="Batch Summary",
                detail=result.get("narrative", ""),
            )


# ─────────────────────────────────────────────────────────────────────────────
# AIEngineService
# ─────────────────────────────────────────────────────────────────────────────

class AIEngineService:

    @staticmethod
    def create_job(
        *,
        batch,
        task_type: str,
        user_intent: str = "",
        user_prompt: str = "",
        system_prompt: str = "",
        target_file=None,
        requested_by=None,
    ):
        from .models import AIAnalysisJob
        return AIAnalysisJob.objects.create(
            batch          = batch,
            task_type      = task_type,
            user_intent    = user_intent,
            user_prompt    = user_prompt,
            system_prompt  = system_prompt or BASE_SYSTEM_PROMPT,
            target_file    = target_file,
            requested_by   = requested_by,
            status         = "queued",
        )

    @staticmethod
    def dispatch(job_id: int) -> None:
        from .models import AIAnalysisJob

        try:
            job = AIAnalysisJob.objects.select_related("batch", "target_file").get(pk=job_id)
        except AIAnalysisJob.DoesNotExist:
            logger.error("AIAnalysisJob %s not found", job_id)
            return

        if job.status != "queued":
            logger.warning("Job %s skipped — status is '%s'", job_id, job.status)
            return

        AIEngineService._run_job(job)

    @staticmethod
    def _run_job(job, on_status_update=None) -> None:
        from tools.services import ToolService

        job.status     = "running"
        job.started_at = datetime.now(tz.utc)
        job.save(update_fields=["status", "started_at"])

        try:
            # Resolve tool whitelist — use task_type as the workflow key,
            # fall back to ALL_TOOL_NAMES for unknown task types.
            tool_names    = WORKFLOW_TOOL_NAMES.get(job.task_type, ALL_TOOL_NAMES)
            system_prompt = job.system_prompt or BASE_SYSTEM_PROMPT
            user_message  = job.user_prompt

            # Inject basic file context so Grok knows what file IDs to use.
            # The actual content is fetched by the read_file tool — we don't
            # dump 12 000 chars of text into every job message any more.
            if job.target_file:
                user_message += (
                    f"\n\n[File available: id={job.target_file.pk} "
                    f"'{job.target_file.original_filename}' "
                    f"type={job.target_file.detected_type or job.target_file.extension}]"
                    f"\nCall detect_file_type({job.target_file.pk}) then "
                    f"read_file({job.target_file.pk}) to access it."
                )
            elif job.batch:
                files = list(job.batch.files.order_by("uploaded_at")[:20])
                if files:
                    listing = "\n".join(
                        f"  file_id={f.pk} '{f.original_filename}' "
                        f"(type={f.detected_type or f.extension})"
                        for f in files
                    )
                    user_message += (
                        f"\n\n[Batch id={job.batch.pk} '{job.batch.label}' "
                        f"has {len(files)} file(s):\n{listing}]\n"
                        f"Call detect_file_type and read_file on each before processing."
                    )

            final_text, tool_call_pks = ToolService.run(
                system_prompt=system_prompt,
                user_message=user_message,
                tool_names=tool_names,
                job=job,
                on_status_update=on_status_update,
            )

            tool_results = [
                tc.result
                for tc in job.tool_calls.filter(status="success").select_related("tool")
                if tc.result
            ]
            _persist_insights(job, tool_results)

            job.raw_response = final_text
            job.status        = "done"
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["raw_response", "status", "finished_at"])

        except Exception as exc:
            logger.exception("Job %s failed: %s", job.pk, exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])

    @staticmethod
    def handle_chat_message(
        *,
        user,
        message: str,
        batch=None,
        workflow=None,
        conversation_history: list[dict] | None = None,
        on_status_update=None,
    ) -> tuple[str, int | None]:
        from tools.services import ToolService

        task_type  = workflow.workflow_type if workflow else "custom"
        tool_names = WORKFLOW_TOOL_NAMES.get(task_type, ALL_TOOL_NAMES) if batch else []

        target_file = None
        files = []
        if batch:
            files = list(batch.files.order_by("uploaded_at"))
            if len(files) == 1:
                target_file = files[0]

        user_prompt = message

        # Inject file references — Grok will call read_file to get content
        if target_file:
            user_prompt += (
                f"\n\n[File: id={target_file.pk} "
                f"'{target_file.original_filename}' "
                f"type={target_file.detected_type or target_file.extension}]"
                f"\nCall read_file({target_file.pk}) to access it."
            )
        elif batch and files:
            listing = "\n".join(
                f"  file_id={f.pk} '{f.original_filename}' "
                f"(type={f.detected_type or f.extension})"
                for f in files
            )
            user_prompt += (
                f"\n\n[Batch id={batch.pk} '{batch.label}' "
                f"has {len(files)} file(s):\n{listing}]"
            )
        elif batch:
            user_prompt += f"\n\n[Batch id={batch.pk}: {batch.label}]"

        system_prompt = _build_system_prompt(workflow=workflow, user_intent=message)

        job = AIEngineService.create_job(
            batch         = batch or _get_or_create_dummy_batch(user),
            task_type     = task_type,
            user_intent   = message,
            user_prompt   = user_prompt,
            system_prompt = system_prompt,
            target_file   = target_file,
            requested_by  = user,
        )

        job.status     = "running"
        job.started_at = datetime.now(tz.utc)
        job.save(update_fields=["status", "started_at"])

        try:
            final_text, tool_call_pks = ToolService.run(
                system_prompt=system_prompt,
                user_message=user_prompt,
                tool_names=tool_names,
                job=job,
                conversation_history=conversation_history,
                on_status_update=on_status_update,
            )

            tool_results = [
                tc.result for tc in job.tool_calls.filter(status="success") if tc.result
            ]
            _persist_insights(job, tool_results)

            job.raw_response = final_text
            job.status        = "done"
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["raw_response", "status", "finished_at"])

            return final_text, job.pk

        except Exception as exc:
            logger.exception("handle_chat_message failed: %s", exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])
            return f"Sorry, something went wrong: {exc}", job.pk

    @staticmethod
    def requeue(job_id: int) -> None:
        from .models import AIAnalysisJob
        from .tasks import run_ai_job_task

        try:
            job = AIAnalysisJob.objects.get(pk=job_id)
        except AIAnalysisJob.DoesNotExist:
            logger.error("Cannot requeue — job %s not found", job_id)
            return

        if job.status not in ("error", "done"):
            logger.warning("Cannot requeue job %s — status is '%s'", job_id, job.status)
            return

        job.status          = "queued"
        job.raw_response    = ""
        job.error_message   = ""
        job.input_tokens    = 0
        job.output_tokens   = 0
        job.started_at      = None
        job.finished_at     = None
        job.structured_output = None
        job.save()
        job.insights.all().delete()
        job.tool_calls.all().delete()

        run_ai_job_task.delay(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_dummy_batch(user):
    from uploads.models import UploadBatch
    batch, _ = UploadBatch.objects.get_or_create(
        label       = "Chat (no files)",
        uploaded_by = user,
        defaults    = {
            "status":      "completed",
            "description": "Auto-created for file-less chat messages.",
        },
    )
    return batch