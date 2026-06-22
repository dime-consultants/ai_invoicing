# ai_engine/services.py
"""
AIEngineService — the brain of the system.

Architecture
------------
Rather than loading tool schemas from the DB and dispatching via dotted
paths (that's tools/services.py), this service passes the actual Python
handler functions directly to the Grok client — exactly the pattern in
the weather-bot reference:

    tools = [get_current_temperature, get_current_wind_speed]
    response = client.chat.completions.create(model=..., messages=..., tools=tools)

Grok inspects each function's signature and docstring to build the tool
schema automatically.  When it calls a tool, we execute the function,
append the result as a "tool" message, and loop until Grok stops calling
tools and returns a plain text answer.

Every tool call is recorded as a tools.ToolCall row, and any structured
findings (anomalies, summary bullets, etc.) are written as AIInsight rows
linked to the AIAnalysisJob.

Job lifecycle
-------------
queued → running → done | error

Entry points
------------
AIEngineService.create_job(...)  — persist a queued job
AIEngineService.dispatch(job_id) — run it synchronously (swap to Celery later)
AIEngineService.handle_chat_message(...)  — called by the WebSocket consumer
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as tz
from typing import Callable

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


GROK_MODEL      = lambda: getattr(settings, "GROK_MODEL",          "grok-3")
MAX_TOKENS      = lambda: getattr(settings, "AI_MAX_TOKENS",        4096)
MAX_TOOL_ROUNDS = lambda: getattr(settings, "AI_MAX_TOOL_ROUNDS",   10)


# ─────────────────────────────────────────────────────────────────────────────
# Tool functions — passed directly to Grok
# ─────────────────────────────────────────────────────────────────────────────
# Grok reads each function's signature + docstring to generate the JSON Schema.
# Keep docstrings precise: describe parameters, accepted values, and what is returned.

def detect_file_type(file_id: int) -> dict:
    """
    Inspect an uploaded file and determine its document type.
    Sets detected_type on the UploadedFile record.
    Always call this first when you receive a new file_id.

    Args:
        file_id: PK of the UploadedFile to inspect.

    Returns:
        dict with keys: ok, file_id, filename, detected_type, confidence.
        detected_type is one of: ura_fiscal_receipt, safaricom_bill,
        acon_export, generic_xlsx, generic_csv, generic_pdf, unknown.
    """
    from tools.handlers import detect_file_type as _h
    return _h(file_id=file_id)


def extract_ura_receipts(file_id: int) -> dict:
    """
    Parse a URA fiscal receipt .txt file (detected_type='ura_fiscal_receipt').
    Extracts every FISCAL RECEIPT and CREDIT NOTE block:
    CU Invoice Number, Date, Time, Total (UGX), Taxes (UGX), Entry Type.
    Saves an .xlsx output file to outputs/converted/.

    Args:
        file_id: PK of the UploadedFile (.txt) to parse.

    Returns:
        dict with keys: ok, record_count, headers, rows (first 5), output_filename, summary.
    """
    from tools.handlers import extract_ura_receipts as _h
    return _h(file_id=file_id)


def extract_kra_receipts(file_id: int) -> dict:
    """
    Parse a KRA fiscal receipt .txt file (detected_type='kra_fiscal_receipt').
    Extracts every RECEIPT block:
    Invoice Number, Date, Time, Total (UGX), Taxes (UGX).
    Saves an .xlsx output file to outputs/converted/.

    Args:
        file_id: PK of the UploadedFile (.txt) to parse.
    Returns:
        dict with keys: ok, record_count, headers, rows (first 5), output_filename, summary.
    """
    from tools.handlers import extract_kra_receipts as _h
    return _h(file_id=file_id)


def extract_safaricom_bill(file_id: int) -> dict:
    """
    Extract line items from a Safaricom monthly telephone bill PDF
    (detected_type='safaricom_bill').
    Columns: Name, Reference NO., Invoice NO., Net Amount, VAT, Excise, Billed Amount.
    Saves an .xlsx output file to outputs/converted/.

    Args:
        file_id: PK of the UploadedFile (.pdf) to parse.

    Returns:
        dict with keys: ok, record_count, headers, rows (first 5), output_filename, summary.
    """
    from tools.handlers import extract_safaricom_bill as _h
    return _h(file_id=file_id)


def clean_acon_export(file_id: int) -> dict:
    """
    Load an ACON sales invoice .xlsx export, normalise headers and values,
    strip empty rows, and save a cleaned .xlsx file.
    Use for files where detected_type='acon_export'.

    Args:
        file_id: PK of the UploadedFile (.xlsx) to clean.

    Returns:
        dict with keys: ok, record_count, headers, rows (first 5), output_filename, summary.
    """
    from tools.handlers import clean_acon_export as _h
    return _h(file_id=file_id)


def reconcile_ura_vs_acon(ura_file_id: int, acon_file_id: int) -> dict:
    """
    Cross-reference URA fiscal receipt records against ACON export records.
    Matches on CU Invoice Number vs ACON item/invoice number.
    Saves a variance .xlsx report to outputs/converted/.

    Args:
        ura_file_id:  PK of the URA fiscal receipt UploadedFile (.txt).
        acon_file_id: PK of the ACON export UploadedFile (.xlsx).

    Returns:
        dict with keys: ok, total_ura, matched, unmatched_ura,
        variance_count, variance_rows (first 10), output_filename, summary.
    """
    from tools.handlers import reconcile_ura_vs_acon as _h
    return _h(ura_file_id=ura_file_id, acon_file_id=acon_file_id)


def flag_anomalies(file_id: int) -> dict:
    """
    Scan invoice/receipt data for anomalies:
    duplicate CU numbers, unusually large totals (>3 std deviations),
    round-number estimates, zero-value entries, fiscal receipts with zero tax.

    Args:
        file_id: PK of the UploadedFile to scan.

    Returns:
        dict with keys: ok, records_scanned, anomaly_count,
        critical, warning, info, anomalies (first 20), summary.
        Each anomaly has: cu_invoice_number, anomaly_type, severity, detail, value, date.
    """
    from tools.handlers import flag_anomalies as _h
    return _h(file_id=file_id)


def generate_report(file_id: int, report_type: str = "ura_sales") -> dict:
    """
    Generate a formatted .xlsx report for a processed file.

    Args:
        file_id:     PK of the UploadedFile to report on.
        report_type: One of 'ura_sales' (default), 'safaricom_dept',
                     'variance_summary', 'acon_summary'.

    Returns:
        dict with keys: ok, report_type, record_count, output_filename, summary.
    """
    from tools.handlers import generate_report as _h
    return _h(file_id=file_id, report_type=report_type)


def summarise_batch(batch_id: int) -> dict:
    """
    Return a structured overview of an UploadBatch:
    file count, detected types, parse status breakdown, error files.
    Call this to give the user an overview of what was uploaded.

    Args:
        batch_id: PK of the UploadBatch to summarise.

    Returns:
        dict with keys: ok, batch_id, batch_label, batch_status,
        total_files, status_counts, type_counts, error_files, summary.
    """
    from tools.handlers import summarise_batch as _h
    return _h(batch_id=batch_id)


# All tools available to the AI — Grok reads their signatures + docstrings
ALL_TOOLS: list[Callable] = [
    detect_file_type,
    extract_ura_receipts,
    extract_safaricom_bill,
    clean_acon_export,
    reconcile_ura_vs_acon,
    flag_anomalies,
    generate_report,
    summarise_batch,
]

# Map workflow_type → subset of tools to expose
WORKFLOW_TOOLS: dict[str, list[Callable]] = {
    "ura_processing":       [detect_file_type, extract_ura_receipts,  flag_anomalies, generate_report],
    "safaricom_processing": [detect_file_type, extract_safaricom_bill, generate_report],
    "acon_processing":      [detect_file_type, clean_acon_export,      generate_report],
    "reconciliation":       [detect_file_type, extract_ura_receipts,   clean_acon_export,
                             reconcile_ura_vs_acon, generate_report],
    "classification":       [detect_file_type, extract_ura_receipts,   flag_anomalies],
    "report_generation":    [detect_file_type, extract_ura_receipts,   extract_safaricom_bill,
                             clean_acon_export, generate_report, summarise_batch],
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
# Tool-calling loop
# ─────────────────────────────────────────────────────────────────────────────

def _run_tool_loop(
    *,
    messages: list[dict],
    tools: list[Callable],
    job=None,                  # AIAnalysisJob | None
) -> tuple[str, int, int]:
    """
    Execute the Grok tool-calling loop.

    Passes Python functions directly to the API — Grok builds the JSON
    Schema from each function's type hints and docstring automatically.

    Returns (final_text, total_input_tokens, total_output_tokens).
    """
    client     = _get_client()
    model      = GROK_MODEL()
    max_tok    = MAX_TOKENS()
    max_rounds = MAX_TOOL_ROUNDS()

    # The xAI/OpenAI SDK cannot serialise raw Python functions — `tools` must be
    # a list of JSON-Schema dicts. Build them from each tool's ToolDefinition
    # (registered via `register_tools`), keeping a name→callable map for dispatch.
    from tools.models import ToolDefinition
    fn_map = {fn.__name__: fn for fn in tools}
    _schema_by_name = {
        d.name: d.to_grok_schema()
        for d in ToolDefinition.objects.filter(name__in=list(fn_map), enabled=True)
    }
    tool_schemas = [_schema_by_name[name] for name in fn_map if name in _schema_by_name]
    missing = [name for name in fn_map if name not in _schema_by_name]
    if missing:
        logger.warning(
            "No enabled ToolDefinition for %s — not offered to the LLM. "
            "Run `manage.py register_tools`.", missing
        )

    input_tokens = output_tokens = 0

    for round_num in range(max_rounds):
        logger.debug("Tool loop round %d/%d", round_num + 1, max_rounds)

        create_kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tok,
            temperature=0.2,
        )
        if tool_schemas:
            create_kwargs["tools"] = tool_schemas
            create_kwargs["tool_choice"] = "auto"

        response = client.chat.completions.create(**create_kwargs)

        input_tokens  += response.usage.prompt_tokens
        output_tokens += response.usage.completion_tokens

        choice  = response.choices[0]
        message = choice.message

        # ── No tool call → final answer ───────────────────────────────
        if not message.tool_calls:
            return message.content or "", input_tokens, output_tokens

        # ── Append assistant message (with tool_calls) to history ─────
        messages.append({
            "role":       "assistant",
            "content":    message.content or "",
            "tool_calls": [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        })

        # ── Execute each requested tool call ──────────────────────────
        fn_map = {fn.__name__: fn for fn in tools}

        for tc in message.tool_calls:
            fn_name   = tc.function.name
            fn        = fn_map.get(fn_name)

            started_at = datetime.now(tz.utc)

            if fn is None:
                result        = {"ok": False, "error": f"Unknown tool: {fn_name}"}
                tc_status     = "error"
                error_message = result["error"]
            else:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                    result    = fn(**arguments)
                    tc_status = "success" if result.get("ok", True) else "error"
                    error_message = result.get("error", "")
                except Exception as exc:
                    result        = {"ok": False, "error": str(exc)}
                    tc_status     = "error"
                    error_message = str(exc)
                    logger.exception("Tool %s raised: %s", fn_name, exc)

            finished_at = datetime.now(tz.utc)

            # Persist ToolCall record
            _record_tool_call(
                fn_name=fn_name,
                arguments=arguments if fn else {},
                result=result,
                status=tc_status,
                error_message=error_message,
                started_at=started_at,
                finished_at=finished_at,
                job=job,
            )

            # Feed result back to Grok as a "tool" role message
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "name":         fn_name,
                "content":      json.dumps(result),
            })

    # ── Max rounds hit — force a final answer ─────────────────────────
    logger.warning("Tool loop hit max rounds (%d) — forcing final answer", max_rounds)
    messages.append({
        "role":    "user",
        "content": "Please provide your final answer based on the tool results above.",
    })
    final = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tok, temperature=0.2,
    )
    input_tokens  += final.usage.prompt_tokens
    output_tokens += final.usage.completion_tokens
    return final.choices[0].message.content or "", input_tokens, output_tokens


def _record_tool_call(
    fn_name: str,
    arguments: dict,
    result: dict,
    status: str,
    error_message: str,
    started_at,
    finished_at,
    job=None,
):
    """Persist a ToolCall record, linked to `job` if provided."""
    try:
        from tools.models import ToolDefinition, ToolCall
        tool_def = ToolDefinition.objects.filter(name=fn_name).first()
        if tool_def:
            ToolCall.objects.create(
                job=job,
                tool=tool_def,
                arguments=arguments,
                result=result,
                status=status,
                error_message=error_message,
                started_at=started_at,
                finished_at=finished_at,
            )
    except Exception as exc:
        # Never let audit logging crash the main flow
        logger.warning("Could not persist ToolCall for %s: %s", fn_name, exc)


def _persist_insights(job, tool_results: list[dict]) -> None:
    """
    Walk tool results and write AIInsight rows for any anomalies or
    summary points returned by flag_anomalies / summarise_batch.
    """
    from .models import AIInsight

    for result in tool_results:
        if not isinstance(result, dict) or not result.get("ok"):
            continue

        # Anomalies from flag_anomalies
        for anomaly in result.get("anomalies", []):
            AIInsight.objects.create(
                job          = job,
                insight_type = "anomaly",
                severity     = anomaly.get("severity", "info"),
                reference_key = anomaly.get("cu_invoice_number", "")[:100],
                title        = anomaly.get("anomaly_type", "Anomaly").replace("_", " ").title(),
                detail       = anomaly.get("detail", ""),
            )

        # Variance rows from reconcile_ura_vs_acon
        for vrow in result.get("variance_rows", []):
            severity = "critical" if vrow.get("status") == "UNMATCHED — not in ACON" else "warning"
            AIInsight.objects.create(
                job          = job,
                insight_type = "variance_explanation",
                severity     = severity,
                reference_key = vrow.get("cu_invoice_number", "")[:100],
                title        = vrow.get("status", "Variance"),
                detail       = (
                    f"URA total: {vrow.get('ura_total')} | "
                    f"ACON amount: {vrow.get('acon_amount')} | "
                    f"Difference: {vrow.get('difference')}"
                ),
            )

        # Summary from summarise_batch
        if result.get("summary") and "batch" in result.get("batch_label", "").lower() or result.get("batch_id"):
            AIInsight.objects.create(
                job          = job,
                insight_type = "summary_point",
                severity     = "info",
                title        = f"Batch summary — {result.get('batch_label', '')}",
                detail       = result.get("summary", ""),
            )


# ─────────────────────────────────────────────────────────────────────────────
# AIEngineService
# ─────────────────────────────────────────────────────────────────────────────

class AIEngineService:

    # ── Create + dispatch ─────────────────────────────────────────────────────

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
        """Core execution — updates job status, runs tool loop, saves results."""
        from .models import AIAnalysisJob

        # ── Mark running ──────────────────────────────────────────────
        job.status     = "running"
        job.started_at = datetime.now(tz.utc)
        job.save(update_fields=["status", "started_at"])

        try:
            # ── Build tool subset based on task_type ──────────────────
            tools = WORKFLOW_TOOLS.get(job.task_type, ALL_TOOLS)

            # ── Build messages ────────────────────────────────────────
            system_prompt = job.system_prompt or BASE_SYSTEM_PROMPT
            user_message  = job.user_prompt

            # Inject file context if a target_file is set
            if job.target_file and job.target_file.extracted_text:
                file_ctx = (
                    f"\n\n[Uploaded file: {job.target_file.original_filename} "
                    f"(id={job.target_file.pk}, type={job.target_file.extension})]\n"
                    f"{job.target_file.extracted_text[:8000]}"
                )
                user_message = user_message + file_ctx

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ]

            # ── Run the tool loop ─────────────────────────────────────
            final_text, in_tok, out_tok = _run_tool_loop(
                messages=messages,
                tools=tools,
                job=job,
            )

            # ── Persist insights from tool results ────────────────────
            tool_results = [
                tc.result
                for tc in job.tool_calls.filter(status="success").select_related("tool")
                if tc.result
            ]
            _persist_insights(job, tool_results)

            # ── Mark done ─────────────────────────────────────────────
            job.raw_response  = final_text
            job.input_tokens  = in_tok
            job.output_tokens = out_tok
            job.status        = "done"
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=[
                "raw_response", "input_tokens", "output_tokens",
                "status", "finished_at",
            ])

        except Exception as exc:
            logger.exception("Job %s failed: %s", job.pk, exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])

    # ── Chat entry point ──────────────────────────────────────────────────────

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
        Called by the WebSocket consumer for every user message.

        Creates an AIAnalysisJob, runs the tool loop, returns
        (response_text, job_id).

        If no batch is attached, runs in conversational mode (no file tools).
        """
        from .models import AIAnalysisJob

        task_type = "custom"
        tools     = ALL_TOOLS

        if workflow:
            task_type = workflow.workflow_type
            tools     = WORKFLOW_TOOLS.get(workflow.workflow_type, ALL_TOOLS)

        # Determine target file from batch if only one file uploaded
        target_file = None
        if batch:
            files = list(batch.files.order_by("uploaded_at"))
            if len(files) == 1:
                target_file = files[0]

        # Build user prompt — include file context if available
        user_prompt = message
        if target_file and target_file.extracted_text:
            user_prompt += (
                f"\n\n[File: {target_file.original_filename} "
                f"id={target_file.pk} type={target_file.detected_type or target_file.extension}]\n"
                f"{target_file.extracted_text[:6000]}"
            )
        elif batch and files:
            # Multiple files: give the agent each file_id so it can detect types
            # and pass the right ids to extractors / reconcile_ura_vs_acon.
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

        system_prompt = _build_system_prompt(
            workflow=workflow,
            user_intent=message,
        )

        # Create job record
        job = AIEngineService.create_job(
            batch         = batch or _get_or_create_dummy_batch(user),
            task_type     = task_type,
            user_intent   = message,
            user_prompt   = user_prompt,
            system_prompt = system_prompt,
            target_file   = target_file,
            requested_by  = user,
        )

        # Build full message list (include conversation history for context)
        messages = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history[-20:])     # last 20 turns
        messages.append({"role": "user", "content": user_prompt})

        # Mark running
        job.status     = "running"
        job.started_at = datetime.now(tz.utc)
        job.save(update_fields=["status", "started_at"])

        try:
            final_text, in_tok, out_tok = _run_tool_loop(
                messages=messages,
                tools=tools if batch else [],   # no file tools without a batch
                job=job,
            )

            tool_results = [
                tc.result
                for tc in job.tool_calls.filter(status="success")
                if tc.result
            ]
            _persist_insights(job, tool_results)

            job.raw_response  = final_text
            job.input_tokens  = in_tok
            job.output_tokens = out_tok
            job.status        = "done"
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=[
                "raw_response", "input_tokens", "output_tokens",
                "status", "finished_at",
            ])

            return final_text, job.pk

        except Exception as exc:
            logger.exception("handle_chat_message failed: %s", exc)
            job.status        = "error"
            job.error_message = str(exc)
            job.finished_at   = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])
            return f"Sorry, something went wrong: {exc}", job.pk

    # ── Requeue ───────────────────────────────────────────────────────────────

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
        job.input_tokens   = 0
        job.output_tokens  = 0
        job.started_at     = None
        job.finished_at    = None
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