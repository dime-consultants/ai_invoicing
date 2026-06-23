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
AIEngineService.dispatch(job_id)         — run it synchronously (swap for Celery later)
AIEngineService.handle_chat_message(...) — called by chat / the WebSocket consumer
AIEngineService.requeue(job_id)          — reset a failed/done job and re-run it
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as tz

from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Grok client (lazy singleton)
# Exported for use by other modules that need a raw client/model name —
# ai_engine/views.py:AIAnalyzeView and tools/universal_extractor.py both
# import these directly, so keep the names even though the tool loop
# itself no longer lives here.
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
# These are ToolDefinition.name strings — the single source of truth for
# what each tool does and accepts lives in tools/handlers.py + the
# ToolDefinition DB rows, not here.
# ─────────────────────────────────────────────────────────────────────────────

ALL_TOOL_NAMES: list[str] = [
    "detect_file_type",
    "extract_ura_receipts",
    "extract_safaricom_bill",
    "clean_acon_export",
    "reconcile_ura_vs_acon",
    "flag_anomalies",
    "generate_report",
    "summarise_batch",
    "extract_file_universal",
]

# Map workflow_type / task_type → subset of tool names to expose
WORKFLOW_TOOL_NAMES: dict[str, list[str]] = {
    "ura_processing":       ["detect_file_type", "extract_ura_receipts", "flag_anomalies", "generate_report"],
    "safaricom_processing": ["detect_file_type", "extract_safaricom_bill", "generate_report"],
    "acon_processing":      ["detect_file_type", "clean_acon_export", "generate_report"],
    "reconciliation":       ["detect_file_type", "extract_ura_receipts", "clean_acon_export",
                              "reconcile_ura_vs_acon", "generate_report"],
    "classification":       ["detect_file_type", "extract_ura_receipts", "flag_anomalies"],
    "report_generation":    ["detect_file_type", "extract_ura_receipts", "extract_safaricom_bill",
                              "clean_acon_export", "generate_report", "summarise_batch"],
}


# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are an AI assistant for Kuehne + Nagel's finance team in Nairobi.
You process invoice and receipt files using the tools available to you.

When a user sends a file_id or batch_id:
1. Call detect_file_type first to identify the document.
2. Choose the correct extraction tool based on detected_type.
3. If the user asked for anomaly detection, call flag_anomalies after extraction.
4. Always call generate_report at the end to produce a downloadable file.
5. Summarise what you found in plain English after all tool calls are complete.

Be concise. Quote specific numbers from tool results. Never invent data."""


def _build_system_prompt(workflow=None, user_intent: str = "") -> str:
    prompt = BASE_SYSTEM_PROMPT
    if workflow and workflow.system_prompt_prefix:
        prompt = workflow.system_prompt_prefix.strip() + "\n\n" + prompt
    if user_intent:
        prompt += f"\n\nUser's specific request: {user_intent}"
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# Insight extraction
# Walk tool results (already persisted as ToolCall rows by ToolService) and
# write AIInsight rows for anything flag_anomalies / reconcile_ura_vs_acon /
# summarise_batch found.
# ─────────────────────────────────────────────────────────────────────────────

def _persist_insights(job, tool_results: list[dict]) -> None:
    from .models import AIInsight

    for result in tool_results:
        if not isinstance(result, dict) or not result.get("ok"):
            continue

        for anomaly in result.get("anomalies", []):
            AIInsight.objects.create(
                job=job,
                insight_type="anomaly",
                severity=anomaly.get("severity", "info"),
                reference_key=anomaly.get("cu_invoice_number", "")[:100],
                title=anomaly.get("anomaly_type", "Anomaly").replace("_", " ").title(),
                detail=anomaly.get("detail", ""),
            )

        for vrow in result.get("variance_rows", []):
            severity = "critical" if vrow.get("status") == "UNMATCHED — not in ACON" else "warning"
            AIInsight.objects.create(
                job=job,
                insight_type="variance_explanation",
                severity=severity,
                reference_key=vrow.get("cu_invoice_number", "")[:100],
                title=vrow.get("status", "Variance"),
                detail=(
                    f"URA total: {vrow.get('ura_total')} | "
                    f"ACON amount: {vrow.get('acon_amount')} | "
                    f"Difference: {vrow.get('difference')}"
                ),
            )

        # Summary from summarise_batch — fixed: previous version's condition
        # had an operator-precedence bug that made it fire on almost any
        # result with a "summary" key, not just batch summaries.
        if result.get("batch_id") and result.get("summary"):
            AIInsight.objects.create(
                job=job,
                insight_type="summary_point",
                severity="info",
                title=f"Batch summary — {result.get('batch_label', '')}",
                detail=result.get("summary", ""),
            )


# ─────────────────────────────────────────────────────────────────────────────
# AIEngineService
# ─────────────────────────────────────────────────────────────────────────────

class AIEngineService:

    # ── Create + dispatch ─────────────────────────────────────────────────

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
        """Create and persist an AIAnalysisJob in 'queued' state."""
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
        """
        Run a queued job synchronously.
        Swap the body of this method for a Celery task call in production:
            run_ai_job.delay(job_id)
        """
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
    def _run_job(job) -> None:
        """Core execution — updates job status, runs the tool loop, saves results."""
        from tools.services import ToolService

        job.status     = "running"
        job.started_at = datetime.now(tz.utc)
        job.save(update_fields=["status", "started_at"])

        try:
            tool_names    = WORKFLOW_TOOL_NAMES.get(job.task_type, ALL_TOOL_NAMES)
            system_prompt = job.system_prompt or BASE_SYSTEM_PROMPT
            user_message  = job.user_prompt

            if job.target_file and job.target_file.extracted_text:
                user_message += (
                    f"\n\n[Uploaded file: {job.target_file.original_filename} "
                    f"(id={job.target_file.pk}, type={job.target_file.extension})]\n"
                    f"{job.target_file.extracted_text[:8000]}"
                )

            final_text, tool_call_pks = ToolService.run(
                system_prompt=system_prompt,
                user_message=user_message,
                tool_names=tool_names,
                job=job,
            )

            tool_results = [
                tc.result
                for tc in job.tool_calls.filter(status="success").select_related("tool")
                if tc.result
            ]
            _persist_insights(job, tool_results)

            job.raw_response = final_text
            job.status        = "done"
            job.finished_at    = datetime.now(tz.utc)
            job.save(update_fields=["raw_response", "status", "finished_at"])

        except Exception as exc:
            logger.exception("Job %s failed: %s", job.pk, exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at    = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])

    # ── Chat entry point ────────────────────────────────────────────────────

    @staticmethod
    def handle_chat_message(
        *,
        user,
        message: str,
        batch=None,
        workflow=None,
        conversation_history: list[dict] | None = None,
    ) -> tuple[str, int | None]:
        """
        Called by the WebSocket consumer (and AIRunAnalysisView) for a
        chat-style message. Creates an AIAnalysisJob, runs the tool loop
        through ToolService, returns (response_text, job_id).

        If no batch is attached, runs in conversational mode (no file tools).
        """
        from tools.services import ToolService

        task_type  = workflow.workflow_type if workflow else "custom"
        tool_names = WORKFLOW_TOOL_NAMES.get(task_type, ALL_TOOL_NAMES) if batch else []

        # Determine target file from batch if only one file uploaded
        target_file = None
        files = []
        if batch:
            files = list(batch.files.order_by("uploaded_at"))
            if len(files) == 1:
                target_file = files[0]

        user_prompt = message
        if target_file and target_file.extracted_text:
            user_prompt += (
                f"\n\n[File: {target_file.original_filename} "
                f"id={target_file.pk} type={target_file.detected_type or target_file.extension}]\n"
                f"{target_file.extracted_text[:6000]}"
            )
        elif batch and files:
            listing = "\n".join(
                f"  - file_id={f.pk} '{f.original_filename}' "
                f"(type={f.detected_type or f.extension})"
                for f in files
            )
            user_prompt += (
                f"\n\n[Batch id={batch.pk} has {len(files)} files. Call detect_file_type "
                f"on each, then the matching extractor; for a URA/KRA-vs-ACON request call "
                f"reconcile_ura_vs_acon with the fiscal file_id and the ACON file_id:\n{listing}]"
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
            )

            tool_results = [
                tc.result for tc in job.tool_calls.filter(status="success") if tc.result
            ]
            _persist_insights(job, tool_results)

            job.raw_response = final_text
            job.status        = "done"
            job.finished_at    = datetime.now(tz.utc)
            job.save(update_fields=["raw_response", "status", "finished_at"])

            return final_text, job.pk

        except Exception as exc:
            logger.exception("handle_chat_message failed: %s", exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at    = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])
            return f"Sorry, something went wrong: {exc}", job.pk

    # ── Requeue ─────────────────────────────────────────────────────────────

    @staticmethod
    def requeue(job_id: int) -> None:
        """Reset a failed/done job back to queued and re-run it."""
        from .models import AIAnalysisJob
        try:
            job = AIAnalysisJob.objects.get(pk=job_id)
        except AIAnalysisJob.DoesNotExist:
            logger.error("Cannot requeue — job %s not found", job_id)
            return

        if job.status not in ("error", "done"):
            logger.warning("Cannot requeue job %s — status is '%s'", job_id, job.status)
            return

        job.status         = "queued"
        job.raw_response   = ""
        job.error_message  = ""
        job.input_tokens    = 0
        job.output_tokens   = 0
        job.started_at      = None
        job.finished_at     = None
        job.structured_output = None
        job.save()
        job.insights.all().delete()
        job.tool_calls.all().delete()

        AIEngineService.dispatch(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_dummy_batch(user):
    """
    Return a placeholder batch for chat messages that have no uploaded files.
    This keeps the FK constraint on AIAnalysisJob.batch satisfied.
    """
    from uploads.models import UploadBatch
    batch, _ = UploadBatch.objects.get_or_create(
        label       = "Chat (no files)",
        uploaded_by = user,
        defaults    = {"status": "completed", "description": "Auto-created for file-less chat messages."},
    )
    return batch