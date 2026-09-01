"""
ToolService — LLM tool-calling execution engine.

This is the ONLY tool-calling loop in the codebase. Both chat/services.py
and ai_engine/services.py call ToolService.run() — neither reimplements it.

Flow
----
1.  Load enabled ToolDefinition rows from the DB and convert them to
    Grok's `tools=[]` schema format.
2.  Send the user message + tool list to Grok.
3.  If Grok returns a tool_call, dispatch based on tool_type:
        builtin          → _resolve_handler() → Python function in tools/handlers.py
        webhook          → _call_webhook()    → HTTP POST/GET to user's URL
        prompt_transform → _call_prompt_transform() → Grok sub-call with user prompt
    Write a ToolCall record, then feed the result back to Grok.
4.  Repeat until Grok returns a plain text response (no more tool calls)
    or we hit AI_MAX_TOOL_ROUNDS.
5.  Return the final text response + list of ToolCall PKs.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as tz
from io import BytesIO
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_grok_client():
    """Lazy singleton — avoids import cost on module load."""
    from openai import OpenAI
    api_key = getattr(settings, "XAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured in settings.")
    return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")


def is_transient_llm_error(exc: Exception) -> bool:
    """
    True for network/rate-limit/server-side Grok errors worth a bounded
    Celery retry (chat.tasks.run_chat_turn_task, ai_engine.tasks.run_ai_job_task,
    ai_engine.services.AIEngineService._run_job).

    ToolService.run() below wraps every Grok API exception in a plain
    RuntimeError (`raise RuntimeError(...) from exc`) so callers only ever see
    that wrapper — the original openai.* exception type survives as
    __cause__, so check both or every Grok-call failure is misclassified as
    non-transient.
    """
    try:
        import openai
        transient_types = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )
        if isinstance(exc, transient_types) or isinstance(exc.__cause__, transient_types):
            return True
    except ImportError:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError))


def _resolve_handler(dotted_path: str):
    """
    Import and return the callable at `dotted_path`.
    e.g. "tools.handlers.extract_ura_receipts"
    """
    module_path, _, func_name = dotted_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"Cannot resolve handler '{dotted_path}': {exc}") from exc


def _scope_tools_to_org(qs, organization):
    """Built-ins (created_by IS NULL) stay visible to everyone; user-defined
    tools are only offered to the LLM if they belong to the caller's org —
    otherwise the agent could be offered, and could invoke, another org's
    webhook/prompt_transform tool."""
    from django.db.models import Q
    if organization is None:
        return qs.filter(created_by__isnull=True)
    return qs.filter(Q(created_by__isnull=True) | Q(organization=organization))


def _load_tool_schemas(tool_names: list[str] | None = None, organization=None) -> list[dict]:
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    qs = _scope_tools_to_org(qs, organization)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return [td.to_grok_schema() for td in qs]


def _load_tool_map(tool_names: list[str] | None = None, organization=None) -> dict:
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    qs = _scope_tools_to_org(qs, organization)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return {td.name: td for td in qs.select_related("user_config")}


def _extract_uploaded_file_id(arguments: dict):
    """Best-effort file_id out of a tool call's arguments, for linking the
    resulting ToolCall back to the file it ran against (file_id_b, the
    second slot on two-file tools, is intentionally not also recorded —
    ToolCall only has one uploaded_file slot). Mirrors tools/views.py's
    identically-named helper, used there for File Manager direct runs —
    duplicated rather than imported to avoid a views->services reverse
    dependency.
    """
    raw = arguments.get("file_id") or arguments.get("file_id_a")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _record_tool_call(
    tool_definition,
    arguments: dict,
    result: Any,
    status: str,
    error_message: str = "",
    started_at=None,
    finished_at=None,
    job=None,
) -> "ToolCall":
    from .models import ToolCall
    organization = getattr(job, "organization", None) or getattr(tool_definition, "organization", None)
    return ToolCall.objects.create(
        job=job,
        tool=tool_definition,
        organization=organization,
        arguments=arguments,
        result=result if isinstance(result, (dict, list)) else {"value": result},
        status=status,
        error_message=error_message,
        started_at=started_at,
        finished_at=finished_at,
        uploaded_file_id=_extract_uploaded_file_id(arguments),
    )


def _safe_prompt_substitute(template: str, **placeholders: str) -> str:
    """
    Replace {name} placeholders without using str.format().

    System prompts often contain JSON examples with curly braces
    (e.g. {"document_type": "..."}). str.format() treats those as
    format fields and raises KeyError. This helper only substitutes
    the explicitly provided placeholder names and leaves every other
    brace pair untouched.
    """
    if not template:
        return ""

    result = template
    for key, value in placeholders.items():
        # Support both {key} and {{key}} styles used in stored prompts.
        # {{key}} MUST be replaced first: replacing {key} first turns "{{key}}"
        # into "{<value>}" and the {{key}} branch can then never match.
        result = result.replace("{{" + key + "}}", value)
        result = result.replace("{" + key + "}", value)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# User-defined tool dispatchers
# ─────────────────────────────────────────────────────────────────────────────

def _call_webhook(config, arguments: dict) -> dict:
    """
    Dispatch a webhook tool.

    POSTs (or GETs) the tool arguments to the user's configured URL.
    Expects the endpoint to return JSON. Any non-2xx response or timeout
    is returned as {"ok": False, "error": "..."} so Grok can report it.

    Security note: webhook_headers can contain auth tokens set by the
    tool creator — these are sent as-is. The is_safe=False flag on webhook
    tools means ToolService will ask for user confirmation before calling.
    """
    import urllib.request
    import urllib.error

    url     = config.webhook_url
    method  = config.webhook_method.upper()
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "ai-invoicing/1.0 (webhook-tool)",
        **{k: str(v) for k, v in (config.webhook_headers or {}).items()},
    }
    timeout = config.webhook_timeout_seconds or 30

    try:
        body = json.dumps(arguments).encode("utf-8") if method == "POST" else None

        if method == "GET" and arguments:
            import urllib.parse
            qs  = urllib.parse.urlencode(
                {k: json.dumps(v) if isinstance(v, (dict, list)) else v
                 for k, v in arguments.items()}
            )
            url = f"{url}?{qs}"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw      = resp.read().decode("utf-8", errors="replace")
            status   = resp.status

        if status >= 400:
            return {
                "ok":     False,
                "error":  f"Webhook returned HTTP {status}",
                "detail": raw[:500],
            }

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Non-JSON response — wrap it so the LLM gets something useful
            payload = {"raw_response": raw[:2000]}

        return {"ok": True, **payload}

    except urllib.error.URLError as exc:
        logger.warning("Webhook call to %s failed: %s", url, exc)
        return {"ok": False, "error": f"Webhook connection error: {exc.reason}"}
    except TimeoutError:
        return {"ok": False, "error": f"Webhook timed out after {timeout}s"}
    except Exception as exc:
        logger.exception("Webhook dispatch failed: %s", exc)
        return {"ok": False, "error": str(exc)}


_SHEET_MARKER_RE = re.compile(r"^=== Sheet: (.+?) ===$", re.MULTILINE)


def _extract_sheet_text(full_text: str, sheet_name: str) -> str:
    """Slice one worksheet's rows out of extracted_text produced with the
    "=== Sheet: <name> ===" markers (uploads/services.py's xlsx/xls extract
    branch). Used by reconcile_datasets' optional sheet_a/sheet_b arguments
    so a single workbook holding both sides of a reconciliation as separate
    tabs (e.g. a combined bank-ledger-and-M-Pesa-statement export) can be
    compared against itself correctly, instead of the whole flattened text
    being fed to both sides of the comparison.

    Falls back to the full text — never an empty string — if there are no
    markers (single-sheet file, or extracted before this marker existed) or
    the requested name doesn't match any sheet, since silently comparing
    nothing is worse than comparing too much.
    """
    matches = list(_SHEET_MARKER_RE.finditer(full_text))
    if not matches:
        return full_text
    wanted = sheet_name.strip().lower()
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == wanted:
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            return full_text[start:end].strip()
    return full_text


def _resolve_file_text(file_id, *, max_chars: int, page_from=None, page_to=None, sheet_name=None) -> dict:
    """
    Load the text of one UploadedFile for injection into a prompt.

    Prefers the cached extracted_text; falls back to reading the stored PDF
    directly so large deferred files still yield content. Returns
    {"text", "full_length", "truncated", "meta"} and never raises.
    """
    text: str = ""
    meta: dict = {}
    extension: str = ""
    try:
        from uploads.models import UploadedFile
        uf   = UploadedFile.objects.get(pk=file_id)
        text = uf.extracted_text or ""
        extension = (uf.extension or "").lower()
        if not text and extension == "pdf" and uf.file:
            try:
                from uploads.services import extract_pdf_page_range
                # Honour an explicit range from the caller; otherwise read the
                # whole document rather than assuming the first few pages.
                live = extract_pdf_page_range(
                    uf.file.path,
                    page_from=int(page_from or 1),
                    page_to=int(page_to) if page_to else None,
                    max_chars=max_chars,
                )
                if live.get("ok"):
                    text = live.get("text") or ""
                    meta = {
                        "page_from":  live.get("page_from"),
                        "page_to":    live.get("page_to"),
                        "page_count": live.get("page_count"),
                    }
            except Exception as live_exc:
                logger.warning(
                    "prompt_transform: live PDF extract failed file_id=%s: %s",
                    file_id, live_exc,
                )
    except Exception as exc:
        logger.warning("prompt_transform: could not load file_id=%s: %s", file_id, exc)

    if sheet_name and text:
        text = _extract_sheet_text(text, sheet_name)

    full_length = len(text)
    truncated   = full_length > max_chars
    if truncated:
        text = text[:max_chars] + (
            f"\n\n[TRUNCATED: showing the first {max_chars:,} of "
            f"{full_length:,} characters. This is a PARTIAL document — "
            f"say so in your answer and do not present totals as complete.]"
        )
    return {"text": text, "full_length": full_length,
            "truncated": truncated, "meta": meta, "extension": extension}


def _resolve_batch_text(batch_id, *, max_chars: int) -> dict:
    """Concatenate the text of every file in a batch, labelled by file."""
    try:
        from uploads.models import UploadedFile
        files = list(
            UploadedFile.objects.filter(batch_id=batch_id).order_by("uploaded_at")
        )
    except Exception as exc:
        logger.warning("prompt_transform: could not load batch_id=%s: %s", batch_id, exc)
        return {"text": "", "full_length": 0, "truncated": False, "meta": {}}

    if not files:
        return {"text": "", "full_length": 0, "truncated": False,
                "meta": {"batch_file_count": 0}}

    per_file = max(1, max_chars // len(files))
    parts, full = [], 0
    for uf in files:
        got = _resolve_file_text(uf.pk, max_chars=per_file)
        full += got["full_length"]
        parts.append(
            f"--- file_id={uf.pk} '{uf.original_filename}' "
            f"({uf.detected_type or uf.extension}) ---\n{got['text']}"
        )
    return {
        "text":        "\n\n".join(parts),
        "full_length": full,
        "truncated":   any(len(p) for p in parts) and full > max_chars,
        "meta":        {"batch_file_count": len(files)},
    }


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split text into sequential pieces of at most chunk_size characters."""
    if not text:
        return []
    return [text[i:i + chunk_size] for i in range(0, len(text), max(1, chunk_size))]


def _parse_json_response(raw_text: str) -> dict | None:
    """Strip an optional ```json fence and parse. Never raises."""
    s = (raw_text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"parse_error": "Response was not valid JSON", "raw": s[:1000]}


# Known count-field -> array-field pairs across the seeded prompt_transform
# tools' output_schemas. After merging, the count is recomputed as len() of
# its paired array so the reported total can never drift from the real
# merged row count, regardless of what each chunk's completion claimed.
_COUNT_ARRAY_PAIRS = {
    "record_count":  "records",     # extract_invoice_data
    "anomaly_count": "anomalies",   # flag_anomalies
    "cleaned_count": "records",     # clean_dataset
}
_NARRATIVE_KEYS = {"narrative", "summary"}


def _merge_structured_chunks(structured_list: list, output_schema: dict | None) -> dict:
    """
    Programmatic (non-LLM) merge of per-chunk structured JSON, keyed by the
    tool's own output_schema — replaces the old approach of asking the LLM
    to re-emit the entire merged result in one capped completion, which is
    exactly what silently collapsed large datasets down to a handful of rows
    no matter how well the input side was chunked.
    """
    if not output_schema or not isinstance(output_schema, dict):
        return {}
    properties = output_schema.get("properties", {}) or {}

    def _merge_value(prop_schema: dict, values: list):
        ptype = (prop_schema or {}).get("type")
        if ptype == "array":
            out = []
            for v in values:
                if isinstance(v, list):
                    out.extend(v)
            return out
        if ptype == "object":
            nested_props = (prop_schema or {}).get("properties", {}) or {}
            dicts = [v for v in values if isinstance(v, dict)]
            all_keys: set = set()
            for d in dicts:
                all_keys.update(d.keys())
            return {
                k: _merge_value(nested_props.get(k, {}), [d.get(k) for d in dicts if k in d])
                for k in all_keys
            }
        if ptype in ("integer", "number"):
            nums = [v for v in values if isinstance(v, (int, float))]
            return sum(nums) if nums else 0
        if ptype == "string":
            for v in values:
                if isinstance(v, str) and v.strip():
                    return v
            return ""
        for v in values:
            if v is not None:
                return v
        return None

    merged = {
        key: _merge_value(prop_schema, [s.get(key) for s in structured_list if isinstance(s, dict)])
        for key, prop_schema in properties.items()
    }

    for count_key, array_key in _COUNT_ARRAY_PAIRS.items():
        if count_key in merged and isinstance(merged.get(array_key), list):
            merged[count_key] = len(merged[array_key])

    chunk_count = len(structured_list)
    for key in merged:
        if key.lower() in _NARRATIVE_KEYS and isinstance(merged[key], str) and merged[key]:
            merged[key] = (
                merged[key].rstrip()
                + f" (Combined from {chunk_count} parts covering the full document — no data was truncated.)"
            )

    return merged


def _map_chunk(
    client, model, chunk_system_prompt_base: str, chunk: str, placeholder: str,
    i: int, total: int, output_schema: dict | None,
) -> tuple[str, dict | None, int, int, str]:
    """Run one chunk's completion. Shared by the single- and two-source
    chunked paths. Returns (raw_text, structured_or_None, in_tokens,
    out_tokens, finish_reason)."""
    chunk_system_prompt = chunk_system_prompt_base + (
        f"\n\nThis is part {i + 1} of {total} of one larger document, split "
        "only because it was too long for a single request. Analyze just "
        "this part and do not claim it is the whole document."
    )
    if output_schema:
        chunk_system_prompt += (
            "\n\nRespond ONLY with valid JSON that matches this schema:\n"
            + json.dumps(output_schema, indent=2)
            + "\nDo not include any text outside the JSON."
        )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": chunk_system_prompt},
                {"role": "user", "content": f"Part {i + 1} of {total}."},
            ],
            temperature=0.1,
            max_tokens=getattr(settings, "PROMPT_TRANSFORM_MAX_TOKENS", 12000),
        )
        raw = response.choices[0].message.content or ""
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        structured = _parse_json_response(raw) if output_schema else None
        return (
            raw, structured,
            getattr(response.usage, "prompt_tokens", 0),
            getattr(response.usage, "completion_tokens", 0),
            finish_reason,
        )
    except Exception as exc:
        logger.warning(
            "prompt_transform: chunk %d/%d failed: %s", i + 1, total, exc,
        )
        return (f"[Part {i + 1} could not be processed: {exc}]", {} if output_schema else None, 0, 0, "error")


# How many times a single chunk may be recursively split when the MODEL'S OWN
# OUTPUT (not the input) hits its token cap — up to 2**_MAX_CHUNK_SPLIT_DEPTH
# pieces for one original chunk in the worst case.
_MAX_CHUNK_SPLIT_DEPTH = 4


def _split_chunk_on_line_boundary(chunk: str) -> tuple[str, str]:
    """Split a chunk roughly in half, preferring a nearby newline so a single
    CSV/table row is never cut across the two halves."""
    mid = len(chunk) // 2
    split_at = chunk.rfind("\n", 0, mid)
    if split_at <= 0:
        split_at = chunk.find("\n", mid)
    if split_at <= 0:
        split_at = mid
    return chunk[:split_at], chunk[split_at:]


def _map_chunk_with_retry(
    client, model, build_prompt_fn, chunk: str, placeholder: str,
    i: int, total: int, output_schema: dict | None, depth: int = 0,
) -> tuple[str, dict | None, int, int]:
    """
    Run one chunk, and if the model's own OUTPUT hit its token cap
    (finish_reason == "length") — meaning this piece of the document produced
    more structured JSON than fits in one completion, not that the input side
    was too big — split this chunk again and recurse until it fits (or we hit
    _MAX_CHUNK_SPLIT_DEPTH).

    This is the output-side counterpart to the input chunking above: splitting
    the input alone doesn't guarantee the resulting JSON fits a fixed output
    budget, since JSON is often more verbose than the source text (e.g. a
    compact CSV row expands into a multi-key JSON object per record). Without
    this, a chunk sized correctly for the INPUT cap could still come back with
    truncated, unparseable JSON — silently contributing zero rows to the merge
    even though chunking itself worked.
    """
    raw, structured, in_tok, out_tok, finish_reason = _map_chunk(
        client, model, build_prompt_fn(chunk), chunk, placeholder, i, total, output_schema,
    )
    if finish_reason == "length" and depth < _MAX_CHUNK_SPLIT_DEPTH and len(chunk) > 200:
        logger.warning(
            "prompt_transform: chunk %d/%d output hit the token cap at split-depth %d "
            "(%d input chars) — splitting further, no data dropped",
            i + 1, total, depth, len(chunk),
        )
        left, right = _split_chunk_on_line_boundary(chunk)
        raw_l, structured_l, in_l, out_l = _map_chunk_with_retry(
            client, model, build_prompt_fn, left, placeholder, i, total, output_schema, depth + 1,
        )
        raw_r, structured_r, in_r, out_r = _map_chunk_with_retry(
            client, model, build_prompt_fn, right, placeholder, i, total, output_schema, depth + 1,
        )
        merged = (
            _merge_structured_chunks([structured_l, structured_r], output_schema)
            if output_schema else None
        )
        return (raw_l + "\n" + raw_r, merged, in_l + in_r, out_l + out_r)
    return raw, structured, in_tok, out_tok


def _run_chunked_prompt_transform(
    config, client, model, output_schema, placeholder: str, kind: str, value, max_chars: int,
) -> dict | None:
    """
    Map-reduce for one oversized document instead of truncating it.

    Splits the FULL (untruncated) text into <=max_chars pieces, runs
    config.system_prompt once per piece ("map"), then MERGES every piece's
    already-parsed structured output in Python — no further LLM call, so the
    row count scales with the document instead of being bottlenecked by one
    fixed-size completion. Used by _call_prompt_transform whenever a
    single-source input (one file_id or one batch_id) exceeds
    PROMPT_TRANSFORM_MAX_CHARS.

    Returns None if, once resolved, the text turns out not to need chunking
    after all (e.g. the cached extracted_text was shorter than expected) —
    the caller falls back to the normal single-shot path in that case.
    """
    if kind == "batch":
        full = _resolve_batch_text(value, max_chars=10**9)
    else:
        full = _resolve_file_text(value, max_chars=10**9)

    chunks = _chunk_text(full["text"], max_chars)
    if len(chunks) <= 1:
        return None

    tool_name = getattr(config, "name", "?")
    logger.info(
        "prompt_transform: chunking tool '%s' input into %d pieces (%d chars total)",
        tool_name, len(chunks), full["full_length"],
    )

    # ── Map ──────────────────────────────────────────────────────────────────
    # Each chunk's call is independent, so they run concurrently instead of
    # one after another — this call is made from Django's synchronous request
    # path (chat/views.py's default sync send), which blocks the caller for
    # its whole duration. N sequential Grok calls turned a single slow-but-
    # complete request into an N-times-slower one; running them in parallel
    # keeps wall-clock time close to that of a single call regardless of N.
    max_workers = min(len(chunks), getattr(settings, "PROMPT_TRANSFORM_MAX_PARALLEL_CHUNKS", 6))

    def _map_one(i: int, chunk: str):
        def build_prompt(c: str) -> str:
            return _safe_prompt_substitute(
                config.system_prompt or "",
                arguments="{}",
                file_text=c if placeholder == "file_text" else "",
                file_a_text=c if placeholder == "file_a_text" else "",
                file_b_text=c if placeholder == "file_b_text" else "",
                batch_text=c if placeholder == "batch_text" else "",
            )
        return _map_chunk_with_retry(
            client, model, build_prompt, chunk, placeholder, i, len(chunks), output_schema,
        )

    raw_results: list[str] = [""] * len(chunks)
    structured_results: list = [None] * len(chunks)
    total_input_tokens = total_output_tokens = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_map_one, i, chunk): i for i, chunk in enumerate(chunks)}
        for future in futures:
            i = futures[future]
            raw, structured, in_tok, out_tok = future.result()
            raw_results[i] = raw
            structured_results[i] = structured if isinstance(structured, dict) else {}
            total_input_tokens += in_tok
            total_output_tokens += out_tok

    # ── Merge (pure Python — no LLM call) ──────────────────────────────────
    if output_schema:
        merged = _merge_structured_chunks(structured_results, output_schema)
        result_text = json.dumps(merged, indent=2)
    else:
        merged = None
        result_text = "\n\n".join(
            f"--- Part {i + 1}/{len(chunks)} ---\n{r}" for i, r in enumerate(raw_results)
        )

    result = {
        "ok":                True,
        "result":            result_text,
        "structured":        merged,
        "input_tokens":      total_input_tokens,
        "output_tokens":     total_output_tokens,
        # Chunk+merge covered the whole document — nothing was dropped.
        "input_truncated":   False,
        "input_full_length": full["full_length"],
        "chunked":           True,
        "chunk_count":       len(chunks),
    }
    return _attach_completeness_check(
        result, tool_name, [(full.get("extension", ""), full["text"])],
    )


def _run_chunked_two_source_prompt_transform(
    config, client, model, output_schema,
    chunked_placeholder: str, other_placeholder: str,
    chunked_value, other_value, max_chars: int,
    unchunked_count_key: str | None = None,
) -> dict | None:
    """
    Map-reduce for a two-source tool (e.g. reconcile_datasets: file_id_a +
    file_id_b) when the larger side is too big for a single request. The
    SMALLER side is resolved in full and passed whole into every chunk's
    prompt; the LARGER side is split into <=max_chars pieces. Merges the
    same way as the single-source path.

    unchunked_count_key: if the schema has a count field describing the
    unchunked (smaller) side's own record count (e.g. "count_b" when B is
    the smaller side), naive summing across chunks would multiply it by the
    chunk count — the caller must NOT rely on the generic sum for that one
    field; this function does not know the true value on its own (only the
    LLM does, from its first chunk's read of the whole smaller side), so it
    takes the FIRST chunk's reported value for that key rather than summing.
    """
    full_chunked = _resolve_file_text(chunked_value, max_chars=10**9)
    full_other   = _resolve_file_text(other_value, max_chars=10**9)

    chunks = _chunk_text(full_chunked["text"], max_chars)
    if len(chunks) <= 1:
        return None

    tool_name = getattr(config, "name", "?")
    logger.info(
        "prompt_transform: two-source chunking tool '%s', %d pieces on %s (%d chars), "
        "%s kept whole (%d chars)",
        tool_name, len(chunks), chunked_placeholder, full_chunked["full_length"],
        other_placeholder, full_other["full_length"],
    )

    max_workers = min(len(chunks), getattr(settings, "PROMPT_TRANSFORM_MAX_PARALLEL_CHUNKS", 6))

    def _map_one(i: int, chunk: str):
        def build_prompt(c: str) -> str:
            return _safe_prompt_substitute(
                config.system_prompt or "",
                arguments="{}",
                file_text="",
                file_a_text=c if chunked_placeholder == "file_a_text" else full_other["text"],
                file_b_text=c if chunked_placeholder == "file_b_text" else full_other["text"],
                batch_text="",
            )
        return _map_chunk_with_retry(
            client, model, build_prompt, chunk, chunked_placeholder, i, len(chunks), output_schema,
        )

    raw_results: list[str] = [""] * len(chunks)
    structured_results: list = [None] * len(chunks)
    total_input_tokens = total_output_tokens = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_map_one, i, chunk): i for i, chunk in enumerate(chunks)}
        for future in futures:
            i = futures[future]
            raw, structured, in_tok, out_tok = future.result()
            raw_results[i] = raw
            structured_results[i] = structured if isinstance(structured, dict) else {}
            total_input_tokens += in_tok
            total_output_tokens += out_tok

    if not output_schema:
        return {
            "ok": True,
            "result": "\n\n".join(f"--- Part {i+1}/{len(chunks)} ---\n{r}" for i, r in enumerate(raw_results)),
            "structured": None,
            "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
            "input_truncated": False,
            "input_full_length": full_chunked["full_length"] + full_other["full_length"],
            "chunked": True, "chunk_count": len(chunks),
        }

    merged = _merge_structured_chunks(structured_results, output_schema)
    # The unchunked side's own record count would be summed once per chunk
    # by the generic merge above (each chunk re-read the whole smaller side
    # and reported its count) — take the first chunk's value instead.
    if unchunked_count_key:
        for s in structured_results:
            if isinstance(s, dict) and unchunked_count_key in s:
                merged[unchunked_count_key] = s[unchunked_count_key]
                break

    result = {
        "ok":                True,
        "result":            json.dumps(merged, indent=2),
        "structured":        merged,
        "input_tokens":      total_input_tokens,
        "output_tokens":     total_output_tokens,
        "input_truncated":   False,
        "input_full_length": full_chunked["full_length"] + full_other["full_length"],
        "chunked":           True,
        "chunk_count":       len(chunks),
    }
    return _attach_completeness_check(
        result, tool_name,
        [(full_chunked.get("extension", ""), full_chunked["text"]),
         (full_other.get("extension", ""), full_other["text"])],
    )


# Extensions whose extracted text is one line per source record (a plain or
# tab-joined line per row) — line-counting them gives an objective, non-LLM
# cross-check on how many records a prompt_transform tool SHOULD have found,
# independent of whatever count the model itself reports. PDFs and
# unstructured/prose formats have no such guarantee, so they're deliberately
# excluded rather than producing a misleading comparison.
_LINE_PER_RECORD_EXTENSIONS = {"csv", "xlsx", "xls"}

# Which field(s) in a tool's own structured output represent "how many source
# records did you find" — used to cross-check against the deterministic line
# count above. A tool not listed here gets no check.
_SOURCE_COUNT_FIELDS_BY_TOOL = {
    "extract_invoice_data": ("record_count",),
    "clean_dataset":        ("original_count",),
    "reconcile_datasets":   ("count_a", "count_b"),
}


def _count_source_lines(text: str) -> int:
    """Non-empty line count — a proxy for row count in line-per-record
    formats (csv/xlsx/xls)."""
    return sum(1 for line in text.splitlines() if line.strip())


def _attach_completeness_check(result: dict, tool_name: str, sources: list[tuple[str, str]]) -> dict:
    """
    Best-effort, non-LLM cross-check that a prompt_transform tool's reported
    record count matches the source file(s) it was actually given — catches
    silent under/over-extraction (fabricated or dropped rows) that a prompt
    instruction alone can't guarantee. `sources` is a list of
    (extension, full_text) pairs for every file this call resolved.

    Never raises and never changes "ok"/"structured" — attaches
    "source_record_count" and "record_count_mismatch" as purely informational
    fields so a caller (chat agent, recon UI) can surface a data-quality
    warning instead of blindly trusting the model's self-reported count.
    """
    count_fields = _SOURCE_COUNT_FIELDS_BY_TOOL.get(tool_name)
    structured = result.get("structured")
    if not count_fields or not isinstance(structured, dict):
        return result

    non_empty = [(ext, text) for ext, text in sources if text]
    if not non_empty or any(ext not in _LINE_PER_RECORD_EXTENSIONS for ext, _ in non_empty):
        # No source, or at least one source isn't a line-per-record format
        # (e.g. a PDF) — the comparison wouldn't be meaningful, skip it.
        return result

    expected = sum(max(0, _count_source_lines(text) - 1) for _, text in non_empty)  # -1 for the header row
    if expected <= 0:
        # A single-line (or empty) "source" gives an unreliable expectation
        # from this line-counting heuristic alone — e.g. a header-only file,
        # or content that merely has a .csv name without real row structure.
        # Skip rather than raise a false-positive mismatch.
        return result

    reported = sum(
        v for f in count_fields
        if isinstance(v := structured.get(f), (int, float))
    )
    result["source_record_count"]   = expected
    result["record_count_mismatch"] = expected != reported
    if expected != reported:
        logger.warning(
            "prompt_transform: '%s' reported %s records but the source has "
            "%s lines (minus header) — possible fabricated or dropped rows",
            tool_name, reported, expected,
        )
    return result


def _call_prompt_transform(config, arguments: dict) -> dict:
    """
    Dispatch a prompt_transform tool.

    Builds a Grok sub-call using the user's system_prompt with safe
    placeholder substitutions, driven by which ids the arguments carry:
        {file_text}    — content of `file_id` (falls back to file A / the batch)
        {file_a_text}  — content of `file_id_a`
        {file_b_text}  — content of `file_id_b`
        {batch_text}   — every file in `batch_id`, labelled per file
        {arguments}    — the full arguments dict as a JSON string

    JSON examples inside the prompt (e.g. {"document_type": "..."}) are
    left intact — we never call str.format() on the whole template.

    The LLM response is returned as {"ok": True, "result": <text>,
    "structured": <parsed JSON if output_schema is set>}, plus
    "input_truncated" / "input_full_length" (and page_from/page_to/page_count
    for a live PDF read) so a partial-input run is distinguishable from a
    whole-document one.

    Input size is capped by settings.PROMPT_TRANSFORM_MAX_CHARS (default 60 000).
    """
    client = _get_grok_client()
    model  = getattr(settings, "GROK_MODEL", "grok-3")

    # ── Resolve document content referenced by the arguments ──────────────────
    # A tool declares which files it wants via its parameters_schema. Only
    # "file_id" used to be honoured, so every multi-file tool (reconcile_datasets
    # with file_id_a/file_id_b, summarise_batch with batch_id) silently received
    # an EMPTY {file_text} and answered from nothing at all.
    #
    # The cap is the only limit on how much of a document a tool sees, and it is
    # split across sources so adding a second file doubles neither the prompt nor
    # the cost. Truncation is always recorded, never silent.
    max_file_chars = getattr(settings, "PROMPT_TRANSFORM_MAX_CHARS", 60_000)

    # placeholder name -> (kind, argument value, sheet name or None)
    # sheet_a/sheet_b let reconcile_datasets compare two tabs of the SAME
    # workbook (e.g. file_id_a == file_id_b, a combined bank-ledger-and-
    # M-Pesa-statement export) — without them, passing the same file id
    # twice resolves the identical flattened text on both sides and the
    # comparison trivially matches everything against itself.
    wanted: list[tuple[str, str, object, object]] = []
    if arguments.get("file_id") is not None:
        wanted.append(("file_text",   "file",  arguments["file_id"], arguments.get("sheet")))
    if arguments.get("file_id_a") is not None:
        wanted.append(("file_a_text", "file",  arguments["file_id_a"], arguments.get("sheet_a")))
    if arguments.get("file_id_b") is not None:
        wanted.append(("file_b_text", "file",  arguments["file_id_b"], arguments.get("sheet_b")))
    if arguments.get("batch_id") is not None:
        wanted.append(("batch_text",  "batch", arguments["batch_id"], None))

    per_source = max(1, max_file_chars // max(1, len(wanted)))

    resolved: dict[str, str] = {}
    extensions: dict[str, str] = {}
    file_meta: dict = {}
    full_lengths: dict[str, int] = {}
    total_full_length = 0
    any_truncated     = False

    for placeholder, kind, value, sheet_name in wanted:
        if kind == "batch":
            got = _resolve_batch_text(value, max_chars=per_source)
        else:
            got = _resolve_file_text(
                value,
                max_chars=per_source,
                page_from=arguments.get("page_from"),
                page_to=arguments.get("page_to"),
                sheet_name=sheet_name,
            )
        resolved[placeholder] = got["text"]
        extensions[placeholder] = got.get("extension", "")
        full_lengths[placeholder] = got["full_length"]
        total_full_length += got["full_length"]
        any_truncated = any_truncated or got["truncated"]
        if got["truncated"]:
            logger.warning(
                "prompt_transform: %s truncated from %d to %d chars for tool '%s'",
                placeholder, got["full_length"], per_source, getattr(config, "name", "?"),
            )
        # Page metadata is only meaningful for a single-document read.
        if got["meta"] and len(wanted) == 1:
            file_meta = got["meta"]

        if not got["text"]:
            logger.warning(
                "prompt_transform: %s resolved to EMPTY for tool '%s' (%s=%s)",
                placeholder, getattr(config, "name", "?"), kind, value,
            )

    # The primary placeholder stays populated for single-file prompts written
    # against the original {file_text} contract.
    if "file_text" not in resolved and "file_a_text" in resolved:
        resolved["file_text"] = resolved["file_a_text"]
    if "file_text" not in resolved and "batch_text" in resolved:
        resolved["file_text"] = resolved["batch_text"]

    output_schema = config.output_schema

    # ── Chunk + merge instead of truncating a single oversized source ──────
    # Single-source (one file_id, or one batch_id): map-reduce the whole
    # thing, merging chunk results in Python (see _run_chunked_prompt_transform).
    if len(wanted) == 1 and any_truncated:
        placeholder, kind, value, _sheet_name = wanted[0]
        chunked = _run_chunked_prompt_transform(
            config, client, model, output_schema, placeholder, kind, value, max_file_chars,
        )
        if chunked is not None:
            return chunked

    # Two-source (file_id_a + file_id_b, e.g. reconcile_datasets): chunk
    # whichever side is larger, keep the smaller side whole in every chunk's
    # prompt. A file+batch or three-way combination isn't handled here — no
    # such tool exists in the current manifest.
    if (
        len(wanted) == 2
        and all(kind == "file" for _, kind, _, _ in wanted)
        and any_truncated
    ):
        (ph_1, _, val_1, _), (ph_2, _, val_2, _) = wanted
        if full_lengths.get(ph_1, 0) >= full_lengths.get(ph_2, 0):
            chunked_placeholder, chunked_value = ph_1, val_1
            other_placeholder, other_value = ph_2, val_2
        else:
            chunked_placeholder, chunked_value = ph_2, val_2
            other_placeholder, other_value = ph_1, val_1

        _COUNT_KEY_BY_PLACEHOLDER = {"file_a_text": "count_a", "file_b_text": "count_b"}
        unchunked_count_key = _COUNT_KEY_BY_PLACEHOLDER.get(other_placeholder)

        chunked = _run_chunked_two_source_prompt_transform(
            config, client, model, output_schema,
            chunked_placeholder, other_placeholder, chunked_value, other_value,
            max_file_chars, unchunked_count_key=unchunked_count_key,
        )
        if chunked is not None:
            return chunked

    args_json = json.dumps(arguments, indent=2, default=str)

    system_prompt = _safe_prompt_substitute(
        config.system_prompt or "",
        arguments=args_json,
        # Every placeholder a prompt may reference must be passed, even when
        # empty — an unsubstituted "{file_b_text}" would reach the model as
        # literal text and read as content.
        file_text=resolved.get("file_text", ""),
        file_a_text=resolved.get("file_a_text", ""),
        file_b_text=resolved.get("file_b_text", ""),
        batch_text=resolved.get("batch_text", ""),
    )
    file_truncated   = any_truncated
    file_full_length = total_full_length

    # If the user supplied an output_schema, ask the LLM to return JSON only
    if output_schema:
        system_prompt += (
            "\n\nRespond ONLY with valid JSON that matches this schema:\n"
            + json.dumps(output_schema, indent=2)
            + "\nDo not include any text outside the JSON."
        )

    user_message = args_json if arguments else "Process the file."

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=0.1,
            max_tokens=getattr(settings, "PROMPT_TRANSFORM_MAX_TOKENS", 12000),
        )
        raw_text = response.choices[0].message.content or ""

        # ── Try to parse as JSON if output_schema was provided ────────────────
        structured = None
        if output_schema:
            s = raw_text.strip()
            fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
            if fence:
                s = fence.group(1).strip()
            try:
                structured = json.loads(s)
            except json.JSONDecodeError:
                # Best effort — return raw text alongside the parse failure note
                structured = {"parse_error": "Response was not valid JSON", "raw": s[:1000]}

        result = {
            "ok":         True,
            "result":     raw_text,
            "structured": structured,
            "input_tokens":  getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
            # Provenance of the input this result was derived from — a caller
            # (or the audit log) can tell a whole-document run from a partial one.
            "input_truncated":   file_truncated,
            "input_full_length": file_full_length,
            **file_meta,
        }
        tool_name = getattr(config, "name", "?")
        sources = [(extensions.get(ph, ""), resolved.get(ph, "")) for ph, _, _, _ in wanted]
        return _attach_completeness_check(result, tool_name, sources)

    except Exception as exc:
        logger.exception("prompt_transform dispatch failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# ToolService
# ─────────────────────────────────────────────────────────────────────────────

class ToolService:

    @staticmethod
    def run(
        *,
        system_prompt: str,
        user_message: str,
        tool_names: list[str] | None = None,
        job=None,
        conversation_history: list[dict] | None = None,
        on_status_update=None,
        organization=None,
    ) -> tuple[str, list[int]]:
        """
        Execute a tool-calling conversation with Grok.

        Parameters
        ----------
        system_prompt        System instruction for the LLM.
        user_message         The user's request (may include extracted file text).
        tool_names            Whitelist of tool names to expose. None = all enabled.
        job                   AIAnalysisJob to link ToolCall records to.
        conversation_history  Previous turns [{role, content}, ...].
        organization          Restricts which user-defined tools the LLM is offered
                               to this org's own (plus all built-ins). None = built-ins only.

        Returns
        -------
        (response_text, tool_call_pks)
        """
        client      = _get_grok_client()
        model       = getattr(settings, "GROK_MODEL",         "grok-3")
        max_tokens  = getattr(settings, "AI_MAX_TOKENS",       12000)
        max_rounds  = getattr(settings, "AI_MAX_TOOL_ROUNDS",  25)

        tool_schemas = _load_tool_schemas(tool_names, organization=organization)
        tool_map     = _load_tool_map(tool_names, organization=organization)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        tool_call_pks: list[int] = []
        input_tokens = output_tokens = 0

        for round_num in range(max_rounds):
            logger.debug("Tool loop round %d/%d", round_num + 1, max_rounds)

            kwargs: dict = dict(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
            )
            if tool_schemas:
                kwargs["tools"]       = tool_schemas
                kwargs["tool_choice"] = "auto"

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc:
                logger.exception("Grok API call failed on round %d: %s", round_num + 1, exc)
                raise RuntimeError(f"Grok API call failed: {exc}") from exc

            try:
                input_tokens  += response.usage.prompt_tokens
                output_tokens += response.usage.completion_tokens
            except Exception:
                pass

            choice  = response.choices[0]
            message = choice.message

            # ── No tool call → final answer ───────────────────────────────────
            if not message.tool_calls:
                if job is not None:
                    job.input_tokens  = (job.input_tokens  or 0) + input_tokens
                    job.output_tokens = (job.output_tokens or 0) + output_tokens
                    job.raw_response  = message.content or ""
                    job.save(update_fields=["input_tokens", "output_tokens", "raw_response"])
                return message.content or "", tool_call_pks

            # ── Append assistant message ──────────────────────────────────────
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

            # ── Execute each tool call ────────────────────────────────────────
            for tc in message.tool_calls:
                tool_name = tc.function.name
                tool_def  = tool_map.get(tool_name)

                if on_status_update:
                    on_status_update("running", tool_name)

                if tool_def is None:
                    error_msg = f"Tool '{tool_name}' is not registered or not enabled."
                    logger.warning(error_msg)
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "name":         tool_name,
                        "content":      json.dumps({"ok": False, "error": error_msg}),
                    })
                    continue

                if not tool_def.is_safe:
                    skip_msg = (
                        f"Tool '{tool_name}' requires explicit user confirmation. "
                        "Please ask the user to confirm before proceeding."
                    )
                    # Record what was actually attempted. This is the one case
                    # that most needs auditing — an unsafe tool (call_webhook,
                    # run_python) the model tried to invoke — and it used to log
                    # arguments={}, so the attempted URL/payload was unknowable.
                    try:
                        attempted_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        attempted_args = {"_unparsed": (tc.function.arguments or "")[:2000]}
                    tc_record = _record_tool_call(
                        tool_def, attempted_args, None,
                        status="skipped",
                        error_message=skip_msg,
                        job=job,
                    )
                    tool_call_pks.append(tc_record.pk)
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "name":         tool_name,
                        "content":      json.dumps({"ok": False, "error": skip_msg}),
                    })
                    continue

                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                started_at = datetime.now(tz.utc)
                try:
                    # ── Three-way dispatch ────────────────────────────────────
                    tool_type = tool_def.tool_type

                    if tool_type == "builtin":
                        handler = _resolve_handler(tool_def.handler)
                        result  = handler(**arguments)

                    elif tool_type == "webhook":
                        try:
                            cfg = tool_def.user_config
                        except Exception:
                            result = {"ok": False, "error": "Webhook tool has no config."}
                        else:
                            result = _call_webhook(cfg, arguments)

                    elif tool_type == "prompt_transform":
                        try:
                            cfg = tool_def.user_config
                        except Exception:
                            result = {"ok": False, "error": "Prompt transform tool has no config."}
                        else:
                            result = _call_prompt_transform(cfg, arguments)

                    else:
                        result = {"ok": False, "error": f"Unknown tool_type '{tool_type}'."}

                    status        = "success" if result.get("ok", True) else "error"
                    error_message = result.get("error", "") if not result.get("ok", True) else ""

                except Exception as exc:
                    result        = {"ok": False, "error": str(exc)}
                    status        = "error"
                    error_message = str(exc)
                    logger.exception("Handler %s raised: %s", tool_def.handler, exc)

                finished_at = datetime.now(tz.utc)

                tc_record = _record_tool_call(
                    tool_def, arguments, result,
                    status=status,
                    error_message=error_message,
                    started_at=started_at,
                    finished_at=finished_at,
                    job=job,
                )
                tool_call_pks.append(tc_record.pk)

                if on_status_update:
                    on_status_update("finished", tool_name)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "name":         tool_name,
                    "content":      json.dumps(result),
                })

        # ── Max rounds — force final answer ──────────────────────────────────
        logger.warning("Tool loop hit max rounds (%d) — forcing final answer", max_rounds)
        messages.append({
            "role":    "user",
            "content": "Please provide your final answer based on the tool results so far.",
        })
        try:
            final      = client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=max_tokens, temperature=0.2,
            )
            final_text = final.choices[0].message.content or ""
            if job is not None:
                job.raw_response = final_text
                job.save(update_fields=["raw_response"])
            return final_text, tool_call_pks
        except Exception as exc:
            logger.exception("Final Grok call failed after max rounds: %s", exc)
            raise RuntimeError(f"Grok final call failed: {exc}") from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Output file collection
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def collect_output_files(tool_call_pks: list[int]) -> list[dict]:
        if not tool_call_pks:
            return []

        from .models import ToolCall

        output_files = []
        seen_paths: set[str] = set()

        for pk in tool_call_pks:
            try:
                tc = ToolCall.objects.select_related("tool").get(pk=pk)
            except ToolCall.DoesNotExist:
                continue

            result = tc.result or {}
            if not result.get("ok"):
                continue

            out_path_str = result.get("output_filename")
            if not out_path_str or out_path_str in seen_paths:
                continue

            out_path = Path(out_path_str)
            if not out_path.exists():
                logger.warning("Tool output file not found on disk: %s", out_path)
                continue

            seen_paths.add(out_path_str)
            try:
                content = out_path.read_bytes()
                suffix  = out_path.suffix.lower()
                output_files.append({
                    "filename": out_path.name,
                    "content":  BytesIO(content),
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if suffix == ".xlsx"
                        else "application/pdf"
                        if suffix == ".pdf"
                        else "text/csv"
                        if suffix == ".csv"
                        else "application/octet-stream"
                    ),
                })
            except Exception as exc:
                logger.warning("Could not read tool output %s: %s", out_path, exc)

        return output_files

    @staticmethod
    def collect_output_files_for_job(job_id: int) -> list[dict]:
        from .models import ToolCall
        pks = list(ToolCall.objects.filter(job_id=job_id).values_list("pk", flat=True))
        return ToolService.collect_output_files(pks)