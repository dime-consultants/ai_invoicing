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

from celery.exceptions import SoftTimeLimitExceeded
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
    "export_file",
    "call_webhook",
    "run_python",
    # domain builtins — deterministic parsers for the known file shapes.
    # Prefer these over the generic prompt_transform equivalents below when the
    # file matches: they do the Safaricom summary→detail CU join and the
    # URA/ACON statutory-column match, neither of which a prompt can do reliably.
    "extract_safaricom_bill",
    "reconcile_ura_vs_acon",
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
        "extract_safaricom_bill",   # use for a Safaricom bill — joins CU numbers
        "extract_invoice_data",
        "write_xlsx",
        "export_file",
    ],
    # Full reconciliation: extract both sides, compare, write report
    "reconciliation": [
        "detect_file_type",
        "read_file",
        "extract_safaricom_bill",
        "extract_invoice_data",
        "reconcile_ura_vs_acon",    # use when one side is ACON — statutory join
        "reconcile_datasets",       # generic fallback for other file pairs
        "write_xlsx",
        "export_file",
    ],
    # Anomaly detection: read, extract, flag
    "anomaly_detection": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "flag_anomalies",
        "write_xlsx",
        "export_file",
    ],
    # Data cleaning: read, clean, optionally write
    "data_cleaning": [
        "detect_file_type",
        "read_file",
        "clean_dataset",
        "write_xlsx",
        "export_file",
    ],
    # Batch overview: summarise all files in a batch
    "batch_summary": [
        "detect_file_type",
        "read_file",
        "summarise_batch",
        "export_file",
    ],
    # Report generation: extract + write formatted xlsx
    "report_generation": [
        "detect_file_type",
        "read_file",
        "extract_invoice_data",
        "flag_anomalies",
        "write_xlsx",
        "export_file",
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
5. If a tool result contains more than about 25 rows/records/anomalies, do NOT
   try to enumerate them in your reply. Call write_xlsx (or export_file, if the
   data came from an already-uploaded file) to produce a downloadable file,
   then summarise: total count, key totals/aggregates, and up to 5 notable
   examples (largest variances, most severe anomalies, etc.). Point the user
   to the downloadable file for the full data.
6. Summarise your findings in plain English after all tool calls complete.

Rules:
- Quote specific numbers from tool results — never invent data.
- If a tool returns ok=false, explain the error and suggest a fix.
- Be concise. Finance users are busy.
- Never try to fit an entire large dataset into your chat reply — chat text
  cannot scale to hundreds of rows; a downloadable file can."""


def _build_system_prompt(workflow=None, user_intent: str = "") -> str:
    prompt = BASE_SYSTEM_PROMPT
    if workflow and getattr(workflow, "system_prompt_prefix", ""):
        prompt = workflow.system_prompt_prefix.strip() + "\n\n" + prompt
    if not user_intent:
        prompt += (
            "\n\nNo specific request was given — the user only attached file(s). "
            "Inspect the file(s) yourself (detect_file_type / read_file) and produce "
            "whichever of a summary, a reconciliation, or a report generation best "
            "fits the data: multiple files that look like two sides of the same "
            "transactions call for reconciliation; a single financial document calls "
            "for a summary or report. State which you chose and why."
        )
    else:
        prompt += f"\n\nUser's specific request: {user_intent}"
    return prompt


def _infer_workflow_from_request(message: str, batch=None):
    """
    Fallback workflow inference for the "no explicit request" path.

    Reached from handle_chat_message() when the caller didn't pass a
    workflow (chat.services.ChatService._resolve_workflow_for_message already
    tried keyword/attachment-count matching and came up empty — e.g. no
    enabled "custom" Workflow row exists, or this was called directly without
    going through ChatService at all).

    Looks at the batch's files (if any) instead of keywords, so a message
    that's just a file with no text still resolves to a sensible workflow —
    reconciliation for 2+ files, summary/report for financial document types —
    instead of leaving the caller with nothing. Delegates to
    chat.services.infer_workflow_from_signals so this can't drift from the
    attachment-driven heuristic. Never raises: a None return is a valid
    outcome, and _build_system_prompt already knows how to proceed without a
    workflow (see the "no specific request" prompt addition above).
    """
    from chat.services import infer_workflow_from_signals

    file_types: list[str] = []
    if batch is not None:
        try:
            file_types = [
                (f.detected_type or f.extension or "").lower()
                for f in batch.files.all()
            ]
        except Exception as exc:
            logger.warning("Could not read batch files for workflow inference: %s", exc)

    try:
        return infer_workflow_from_signals(message, file_types)
    except Exception as exc:
        logger.warning("Workflow inference failed (batch=%s): %s", getattr(batch, "pk", None), exc)
        return None


def _collect_conversation_files(conversation) -> list:
    """
    Every UploadedFile ever attached to any message in `conversation`, deduped
    by UploadedFile pk, oldest first.

    Powers cross-turn file visibility in handle_chat_message: a file uploaded
    in turn 1 must stay usable (readable, exportable) in turn 5 without the
    user re-attaching it. Returns [] for a None conversation (the fully
    stateless callers that don't pass one) or on any lookup error — never
    raises, since a failure here should degrade to "no extra files visible
    this turn," not break the turn.
    """
    if conversation is None:
        return []
    try:
        from chat.models import ChatMessageAttachment
        attachments = (
            ChatMessageAttachment.objects
            .filter(message__conversation=conversation)
            .exclude(uploaded_file__isnull=True)
            .select_related("uploaded_file")
            .order_by("created_at")
        )
        seen: dict = {}
        for att in attachments:
            uf = att.uploaded_file
            if(
                uf
                and uf.parse_status == "parsed"
                and uf.pk not in seen
            ):
                seen[uf.pk] = uf
                
        return list(seen.values())
    except Exception as exc:
        logger.warning(
            "Could not collect conversation files (conversation=%s): %s",
            getattr(conversation, "pk", None), exc,
        )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Insight extraction
# ─────────────────────────────────────────────────────────────────────────────

def _persist_insights(job, tool_results: list[dict]) -> None:
    """
    Walk tool results and write AIInsight rows for anything flag_anomalies,
    reconcile_datasets, or summarise_batch produced.

    Builtin handlers (e.g. reconcile_ura_vs_acon) return their data at the
    top level of the result dict. prompt_transform tools do not — Tool
    Service.run() stores _call_prompt_transform()'s return value verbatim as
    ToolCall.result, and that wrapper is {"ok", "result": <raw text>,
    "structured": {...the actual parsed rows/anomalies/narrative...}, ...}.
    Reading straight off the top level (as this used to do) means "rows",
    "anomalies", and "narrative" are never found for any prompt_transform
    tool — i.e. every LLM-driven reconciliation/anomaly/summary workflow —
    so no insight was ever created regardless of what the LLM returned.
    Prefer "structured" when present; fall back to the top level for
    builtin-handler results, which have no such wrapper.
    """
    from .models import AIInsight

    for result in tool_results:
        if not isinstance(result, dict) or not result.get("ok"):
            continue

        structured = result.get("structured")
        payload = structured if isinstance(structured, dict) else result

        # flag_anomalies result
        for anomaly in payload.get("anomalies", []):
            AIInsight.objects.create(
                job=job,
                insight_type="anomaly",
                severity=anomaly.get("severity", "info"),
                reference_key=str(anomaly.get("id", ""))[:100],
                title=anomaly.get("anomaly_type", "Anomaly").replace("_", " ").title(),
                detail=anomaly.get("detail", ""),
            )

        # reconcile_datasets result
        for row in payload.get("rows", []):
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
        if payload.get("narrative"):
            AIInsight.objects.create(
                job=job,
                insight_type="summary_point",
                severity="info",
                title="Batch Summary",
                detail=payload.get("narrative", ""),
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
        organization = (
            getattr(requested_by, "organization", None)
            or getattr(batch, "organization", None)
        )
        return AIAnalysisJob.objects.create(
            batch          = batch,
            task_type      = task_type,
            user_intent    = user_intent,
            user_prompt    = user_prompt,
            system_prompt  = system_prompt or BASE_SYSTEM_PROMPT,
            target_file    = target_file,
            requested_by   = requested_by,
            organization   = organization,
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
                organization=job.organization,
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

        except SoftTimeLimitExceeded:
            # Don't swallow this into the generic error handler below — it
            # would overwrite job.error_message with an unhelpful empty
            # string and skip run_ai_job_task's dedicated "processing took
            # too long, try a smaller batch" message. Let it propagate.
            raise
        except Exception as exc:
            logger.exception("Job %s failed: %s", job.pk, exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])

            # Re-raise transient failures (Grok rate limits/connection errors)
            # so run_ai_job_task's Celery-level retry actually fires. Anything
            # else stays swallowed here — the job row above is already the
            # terminal, user-visible record of what happened, and dispatch()
            # returning normally is what callers of dispatch() expect.
            from tools.services import is_transient_llm_error
            if is_transient_llm_error(exc):
                raise

    @staticmethod
    def handle_chat_message(
        *,
        user,
        message: str,
        batch=None,
        workflow=None,
        conversation_history: list[dict] | None = None,
        conversation=None,
        on_status_update=None,
    ) -> tuple[str, int | None]:
        from tools.services import ToolService

        if workflow is None:
            workflow = _infer_workflow_from_request(message=message, batch=batch)

        task_type = workflow.workflow_type if workflow else "custom"

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

        # Conversation-wide file visibility — every file ever attached anywhere
        # in this conversation, not just this turn's. Without this, a
        # follow-up turn that doesn't re-attach a file has no way to learn
        # which file_id an earlier turn's upload got: conversation history is
        # rebuilt from plain ChatMessage.content
        # (chat.services.ChatService.build_conversation_history) and never
        # contains the "[File: id=...]" hint injected above — that hint lives
        # only in this one call's user_prompt and is never persisted.
        # Recomputed fresh on every turn (never stored), so it always reflects
        # the current DB state, including files uploaded after this turn's
        # conversation_history snapshot was taken.
        conversation_files = _collect_conversation_files(conversation)
        current_turn_ids = {f.pk for f in files}
        other_files = [f for f in conversation_files if f.pk not in current_turn_ids]
        if other_files:
            listing = "\n".join(
                f"  file_id={f.pk} '{f.original_filename}' "
                f"(type={f.detected_type or f.extension})"
                for f in other_files
            )
            user_prompt += (
                f"\n\n[Other files available from earlier in this conversation "
                f"({len(other_files)}):\n{listing}]\n"
                f"Call read_file(file_id) or export_file(file_id, target_format) "
                f"on any of these if the user's request refers to them, even "
                f"though they weren't attached to this message."
            )

        # Tools (read_file, export_file, etc.) are only useful when there's
        # something to read — but "something to read" now means this turn's
        # batch OR any file anywhere in the conversation, not just this turn's
        # batch. Previously this was `if batch else []`, and since an empty
        # list is falsy in the tool_names filter (tools/services.py's
        # _load_tool_map/_load_tool_schemas), a file-less turn silently got
        # ALL tools anyway — this makes the condition match what the code
        # actually needs instead of working by accident.
        has_any_files = bool(batch) or bool(conversation_files)
        tool_names = WORKFLOW_TOOL_NAMES.get(task_type, ALL_TOOL_NAMES) if has_any_files else []

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
                organization=job.organization,
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

        # Prior insights/tool_calls are already gone; if nothing gets queued the
        # job would sit at "queued" forever with its results destroyed.
        from config.dispatch import dispatch
        if dispatch(run_ai_job_task, job_id) is None:
            job.status        = "error"
            job.error_message = "Could not queue the job — the task broker is unavailable."
            job.save(update_fields=["status", "error_message"])


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