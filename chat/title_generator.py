# chat/title_generator.py
"""
Auto-generate conversation titles using Grok.

Two entry points:
  generate_title_from_user_input(text)  — fast, single-message title
  generate_conversation_title(messages) — richer, uses first 6 turns

Both fall back gracefully when XAI_API_KEY is unset or the call fails.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert at creating concise, descriptive titles for conversations. "
    "Generate a short title (3–7 words) that captures the main topic or intent. "
    "Return ONLY the title text — no quotes, no explanation, no punctuation at the end."
)


def _call_grok(user_prompt: str) -> Optional[str]:
    """Make one Grok call and return the stripped title, or None on failure."""
    try:
        from ai_engine.services import _get_client, GROK_MODEL
        client = _get_client()
        # GROK_MODEL is a callable lambda — always call it
        model  = GROK_MODEL() if callable(GROK_MODEL) else GROK_MODEL
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=30,
        )
        raw = response.choices[0].message.content or ""
        title = raw.strip().strip('"\'').split("\n")[0].strip()
        return title if title else None
    except Exception as exc:
        logger.warning("Title generation failed: %s", exc)
        return None


def generate_title_from_user_input(user_input: str) -> Optional[str]:
    """
    Generate a title from the user's first message.
    Falls back to a truncated version of the input if Grok is unavailable.
    """
    if not user_input or not user_input.strip():
        return None

    text = user_input.strip()

    # Short inputs can be used directly
    if len(text) <= 50:
        return text

    title = _call_grok(
        f"Based on this user input, generate a concise title:\n\n{text[:300]}"
    )
    return title or text[:50].rstrip()


def generate_conversation_title(messages: list[dict]) -> Optional[str]:
    """
    Generate a title from the first few turns of a conversation.
    """
    if not messages:
        return None

    summary = "\n".join(
        f"{m.get('role', 'unknown').upper()}: {m.get('content', '')[:200]}"
        for m in messages[:6]
    )

    return _call_grok(
        f"Based on this conversation, generate a concise title:\n\n{summary}"
    )