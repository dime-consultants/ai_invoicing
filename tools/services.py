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


def _load_tool_schemas(tool_names: list[str] | None = None) -> list[dict]:
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return [td.to_grok_schema() for td in qs]


def _load_tool_map(tool_names: list[str] | None = None) -> dict:
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return {td.name: td for td in qs.select_related("user_config")}


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
    return ToolCall.objects.create(
        job=job,
        tool=tool_definition,
        arguments=arguments,
        result=result if isinstance(result, (dict, list)) else {"value": result},
        status=status,
        error_message=error_message,
        started_at=started_at,
        finished_at=finished_at,
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
        # Support both {key} and {{key}} styles used in stored prompts
        result = result.replace("{" + key + "}", value)
        result = result.replace("{{" + key + "}}", value)
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


def _call_prompt_transform(config, arguments: dict) -> dict:
    """
    Dispatch a prompt_transform tool.

    Builds a Grok sub-call using the user's system_prompt with safe
    placeholder substitutions:
        {file_text}   — extracted text of the target file (if file_id in arguments)
        {arguments}   — the full arguments dict as a JSON string

    JSON examples inside the prompt (e.g. {"document_type": "..."}) are
    left intact — we never call str.format() on the whole template.

    The LLM response is returned as {"ok": True, "result": <text>,
    "structured": <parsed JSON if output_schema is set>}.
    """
    client = _get_grok_client()
    model  = getattr(settings, "GROK_MODEL", "grok-3")

    # ── Resolve file_text if file_id is in arguments ──────────────────────────
    file_text = ""
    file_id   = arguments.get("file_id")
    if file_id:
        try:
            from uploads.models import UploadedFile
            uf        = UploadedFile.objects.get(pk=file_id)
            file_text = uf.extracted_text or ""
            # If still pending / empty, try a live page-range extract for PDFs
            # so prompt_transform tools still get content on large deferred files.
            if not file_text and (uf.extension or "").lower() == "pdf" and uf.file:
                try:
                    from uploads.services import extract_pdf_page_range
                    live = extract_pdf_page_range(
                        uf.file.path,
                        page_from=1,
                        page_to=min(10, uf.page_count or 10),
                        max_chars=8000,
                    )
                    if live.get("ok"):
                        file_text = live.get("text") or ""
                except Exception as live_exc:
                    logger.warning(
                        "prompt_transform: live PDF extract failed file_id=%s: %s",
                        file_id, live_exc,
                    )
        except Exception as exc:
            logger.warning("prompt_transform: could not load file_id=%s: %s", file_id, exc)

    args_json = json.dumps(arguments, indent=2, default=str)

    system_prompt = _safe_prompt_substitute(
        config.system_prompt or "",
        file_text=file_text[:8000],
        arguments=args_json,
    )

    # If the user supplied an output_schema, ask the LLM to return JSON only
    output_schema = config.output_schema
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
            max_tokens=4096,
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

        return {
            "ok":         True,
            "result":     raw_text,
            "structured": structured,
            "input_tokens":  getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
        }

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

        Returns
        -------
        (response_text, tool_call_pks)
        """
        client      = _get_grok_client()
        model       = getattr(settings, "GROK_MODEL",         "grok-3")
        max_tokens  = getattr(settings, "AI_MAX_TOKENS",       4096)
        max_rounds  = getattr(settings, "AI_MAX_TOOL_ROUNDS",  10)

        tool_schemas = _load_tool_schemas(tool_names)
        tool_map     = _load_tool_map(tool_names)

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
                    _record_tool_call(
                        tool_def, {}, None,
                        status="skipped",
                        error_message=skip_msg,
                        job=job,
                    )
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