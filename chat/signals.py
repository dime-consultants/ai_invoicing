# chat/signals.py
"""
Django signals for the chat app.

Fires WebSocket events when:
  - A new assistant ChatMessage is saved via the REST API or a background
    task (NOT via the WS consumer or Celery chat task, which push directly
    to the group — see _WS_CREATED_FLAG below).
  - A new ChatConversation is created → notify the user.

Thread-local flag
-----------------
Both the WS consumer (ChatStreamConsumer.receive) and the Celery task
(run_chat_turn_task) call mark_ws_origin() before saving the assistant
message and clear_ws_origin() immediately after. This suppresses the
signal-based push so the same content isn't delivered twice.
"""
import threading
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

_local = threading.local()


def mark_ws_origin():
    """
    Call before saving an assistant message inside a WS consumer or Celery
    chat task to suppress the signal-based push (the caller already streamed
    the response directly).
    """
    _local.ws_origin = True


def clear_ws_origin():
    """Reset the flag after the save."""
    _local.ws_origin = False


def _is_ws_origin() -> bool:
    return getattr(_local, "ws_origin", False)


@receiver(post_save, sender="chat.ChatMessage")
def on_message_saved(sender, instance, created, **kwargs):
    """
    Push a new assistant message to the conversation's WS group when the
    message was NOT created inside a WS consumer or Celery chat task.

    This covers the REST send endpoint (ChatMessageSendView) where the
    response is generated synchronously and persisted without streaming.
    """
    if not created:
        return
    if instance.role != "assistant":
        return
    if _is_ws_origin():
        return

    try:
        from chat.notify import push_chat_chunk, push_chat_done
        conv_id = instance.conversation_id
        push_chat_chunk(conv_id, content=instance.content or "", turn_id="")
        push_chat_done(conv_id, message_id=instance.pk, turn_id="")
    except Exception as exc:
        logger.warning("on_message_saved WS push failed: %s", exc)


@receiver(post_save, sender="chat.ChatConversation")
def on_conversation_created(sender, instance, created, **kwargs):
    """Notify the user when a new conversation is created via the REST API."""
    if not created:
        return
    user = instance.user
    if not user:
        return
    try:
        from chat.notify import notify_user
        notify_user(
            user.pk,
            title="New conversation",
            message=f"Conversation '{instance.title}' was created.",
        )
    except Exception as exc:
        logger.warning("on_conversation_created notify failed: %s", exc)