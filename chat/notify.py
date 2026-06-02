# chat/notify.py
"""
Helper functions for pushing real-time events to connected WebSocket clients.

Usage (from any Django view, signal, or Celery task):

    from chat.notify import notify_user, notify_job_update, notify_file_processed

    # Push a generic notification
    notify_user(user_id=42, title="Report Ready", message="Your report has been generated.")

    # Push an AI job status update
    notify_job_update(user_id=42, job_id=7, status="done", title="Anomaly detection complete")

    # Push a file-processed event
    notify_file_processed(user_id=42, file_id=15, filename="receipts.txt", status="parsed")

    # Push a chat stream chunk to a conversation
    from chat.notify import push_chat_chunk, push_chat_done
    push_chat_chunk(conversation_id=3, content="Hello ")
    push_chat_done(conversation_id=3, message_id=99)
"""
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_layer():
    """Return the channel layer, or None if Channels is not configured."""
    try:
        from channels.layers import get_channel_layer
        return get_channel_layer()
    except Exception:
        return None


def _send(group: str, payload: dict) -> bool:
    """
    Synchronously send a message to a channel group.
    Returns True on success, False if the layer is unavailable.
    """
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


# ── Public API ────────────────────────────────────────────────────────────────

def notify_user(user_id, *, title: str, message: str, notification_id: str = "") -> bool:
    """Push a generic notification to a user's notification channel."""
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
    """Push an AI job status update to the user's notification channel."""
    return _send(
        f"notifications_{user_id}",
        {
            "type":   "job_update",
            "job_id": job_id,
            "status": status,
            "title":  title or f"Job {job_id} is now {status}",
        },
    )


def notify_file_processed(user_id, *, file_id, filename: str, status: str = "parsed") -> bool:
    """Push a file-processed event to the user's notification channel."""
    return _send(
        f"notifications_{user_id}",
        {
            "type":     "file_processed",
            "file_id":  file_id,
            "filename": filename,
            "status":   status,
        },
    )


def push_chat_chunk(conversation_id, *, content: str) -> bool:
    """Push a streaming text chunk to a chat conversation channel."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":    "stream_chunk",
            "content": content,
        },
    )


def push_chat_done(conversation_id, *, message_id) -> bool:
    """Signal that streaming is complete for a chat conversation."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":       "stream_done",
            "message_id": str(message_id),
        },
    )


def push_chat_error(conversation_id, *, message: str) -> bool:
    """Push an error to a chat conversation channel."""
    return _send(
        f"chat_{conversation_id}",
        {
            "type":    "stream_error",
            "message": message,
        },
    )
