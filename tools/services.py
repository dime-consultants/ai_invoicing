# tools/services.py
"""
ToolService — LLM tool-calling execution engine.

Flow
----
1.  Load enabled ToolDefinition rows from the DB and convert them to
    Grok's `tools=[]` schema format.
2.  Send the user message + tool list to Grok.
3.  If Grok returns a tool_call, look up the handler, execute it,
    write a ToolCall record, then feed the result back to Grok.
4.  Repeat until Grok returns a plain text response (no more tool calls)
    or we hit AI_MAX_TOOL_ROUNDS.
5.  Return the final text response + list of ToolCall PKs.

The caller (ai_engine.services) passes in the AIAnalysisJob so every
ToolCall can be linked to it for traceability.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime, timezone as tz
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
    """
    Return Grok-compatible tool schemas for all enabled ToolDefinitions.
    If tool_names is provided, only include those tools.
    """
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return [td.to_grok_schema() for td in qs]


def _load_tool_map(tool_names: list[str] | None = None) -> dict:
    """
    Return {tool_name: ToolDefinition} for all enabled tools.
    """
    from .models import ToolDefinition
    qs = ToolDefinition.objects.filter(enabled=True)
    if tool_names:
        qs = qs.filter(name__in=tool_names)
    return {td.name: td for td in qs}


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
    """Persist a ToolCall record and return it."""
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
        job=None,                          # ai_engine.models.AIAnalysisJob | None
        conversation_history: list[dict] | None = None,
    ) -> tuple[str, list[int]]:
        """
        Execute a tool-calling conversation with Grok.

        Parameters
        ----------
        system_prompt        System instruction for the LLM.
        user_message         The user's request (may include extracted file text).
        tool_names           Whitelist of tool names to expose. None = all enabled.
        job                  AIAnalysisJob to link ToolCall records to.
        conversation_history Previous turns [{role, content}, ...].

        Returns
        -------
        (response_text, tool_call_pks)
        response_text   — final plain-text response from Grok
        tool_call_pks   — PKs of every ToolCall record created this run
        """
        client      = _get_grok_client()
        model       = getattr(settings, "GROK_MODEL",          "grok-3")
        max_tokens  = getattr(settings, "AI_MAX_TOKENS",        4096)
        max_rounds  = getattr(settings, "AI_MAX_TOOL_ROUNDS",   10)

        tool_schemas = _load_tool_schemas(tool_names)
        tool_map     = _load_tool_map(tool_names)

        # ── Build initial message list ────────────────────────────────
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        tool_call_pks: list[int] = []
        input_tokens = output_tokens = 0

        # ── Tool-calling loop ─────────────────────────────────────────
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

            response = client.chat.completions.create(**kwargs)

            input_tokens  += response.usage.prompt_tokens
            output_tokens += response.usage.completion_tokens

            choice  = response.choices[0]
            message = choice.message

            # ── No tool call → final answer ───────────────────────────
            if not message.tool_calls:
                # Update job token counts if we have one
                if job is not None:
                    job.input_tokens  = (job.input_tokens  or 0) + input_tokens
                    job.output_tokens = (job.output_tokens or 0) + output_tokens
                    job.raw_response  = message.content or ""
                    job.save(update_fields=["input_tokens", "output_tokens", "raw_response"])

                return message.content or "", tool_call_pks

            # ── Append assistant message with tool_calls to history ───
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

            # ── Execute each tool call ────────────────────────────────
            for tc in message.tool_calls:
                tool_name = tc.function.name
                tool_def  = tool_map.get(tool_name)

                # Unknown tool
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

                # Unsafe tool — skip execution, tell Grok
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

                # Parse arguments
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                # Execute
                started_at = datetime.now(tz.utc)
                try:
                    handler = _resolve_handler(tool_def.handler)
                    result  = handler(**arguments)
                    status  = "success" if result.get("ok", True) else "error"
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

                # Feed result back to Grok
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "name":         tool_name,
                    "content":      json.dumps(result),
                })

        # ── Max rounds hit — ask Grok to summarise what it found ──────
        logger.warning("Tool loop hit max rounds (%d) — forcing final answer", max_rounds)
        messages.append({
            "role":    "user",
            "content": "Please provide your final answer based on the tool results so far.",
        })
        final = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return final.choices[0].message.content or "", tool_call_pks