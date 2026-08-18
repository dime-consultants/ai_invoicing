# chat/notify.py
"""
Helper functions for pushing real-time events to connected WebSocket clients.

All public functions are synchronous and safe to call from Django views,
signals, and Celery tasks.

Usage:

    from chat.notify import notify_user, notify_job_update, push_chat_chunk

    notify_user(42, title="Report Ready", message="Your report has been generated.")
    notify_job_update(42, job_id=7, status="done", title="Anomaly detection complete")
    push_chat_chunk(3, content="Hello ", turn_id="<uuid>")
    push_chat_done(3, message_id=99, turn_id="<uuid>")
"""
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)

def _get_layer():
    try:
        from channels.layers import get_channel_layer
        return get_channel_layer()
    except Exception:
        return None


def _send(group: str, payload: dict) -> bool:
    layer = _get_layer()
    if layer is None:
        logger.debug("Channel layer unavailable — skipping WS push to %s", group)
        return False
    try:
        from asgiref.sync import async_to_sync
        async_to_sync(layer.group_send)(group, payload)
        return True
    except Exception as exc:
        logger.warning("WS push to group %s failed: %s", group, exc)
        return False


# ── Notification channel ──────────────────────────────────────────────────────

def notify_user(user_id, *, title: str, message: str, notification_id: str = "") -> bool:
    import uuid
    return _send(
        f"notifications_{user_id}",
        {
            "type":      "push_notification",
            "id":        notification_id or str(uuid.uuid4()),
            "title":     title,
            "message":   message,
            "timestamp": timezone.now().isoformat(),
        },
    )


def notify_job_update(user_id, *, job_id, status: str, title: str = "") -> bool:
    return _send(
        f"notifications_{user_id}",
        {
            "type":   "job_update",
            "job_id": job_id,
            "status": status,
            "title":  title or f"Job {job_id} is now {status}",
        },
    )


def notify_file_processed(user_id, *, file_id, filename: str, status: str = "parsed", **extra) -> bool:
    """
    Notify that a file was processed. Accepts optional extra kwargs (e.g.
    `page_count`, `extraction_deferred`) and includes them in the payload if
    present. Backwards-compatible with older callers that only pass the core
    arguments.
    """
    payload = {
        "type":     "file_processed",
        "file_id":  file_id,
        "filename": filename,
        "status":   status,
    }
    # Preserve commonly-sent extras, but allow any keys through for forward
    # compatibility with other callers.
    if extra:
        payload.update(extra)

    return _send(f"notifications_{user_id}", payload)


# ── Chat streaming channel ────────────────────────────────────────────────────

def push_chat_chunk(conversation_id, *, content: str, turn_id: str = "") -> bool:
    """Push a streaming text chunk to the chat conversation group."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":    "stream_chunk",
            "content": content,
            "turn_id": turn_id,
        },
    )


def push_chat_done(
    conversation_id,
    *,
    message_id,
    turn_id: str = "",
    attachments: list | None = None,
) -> bool:
    """Signal that streaming is complete for a chat turn."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":        "stream_done",
            "message_id":  str(message_id),
            "turn_id":     turn_id,
            "attachments": attachments or [],
        },
    )


def push_chat_error(conversation_id, *, message: str, turn_id: str = "") -> bool:
    """Push an error to the chat conversation group."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":    "stream_error",
            "message": message,
            "turn_id": turn_id,
        },
    )


def push_chat_status(conversation_id, *, status: str, tool_name: str = "", turn_id: str = "") -> bool:
    """Push a tool execution status update to the chat conversation group."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":      "stream_status",
            "status":    status,
            "tool_name": tool_name,
            "turn_id":   turn_id,
        },
    )