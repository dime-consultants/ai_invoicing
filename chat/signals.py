# chat/signals.py
"""
Django signals for the chat app.

Fires WebSocket events when:
  - A new assistant ChatMessage is saved via the REST API or a background task
    (NOT via the WS consumer, which streams directly — see _WS_CREATED_FLAG).
  - A new ChatConversation is created → notify the user.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

# Thread-local flag set by ChatStreamConsumer.receive() to suppress the
# signal-based push (the consumer already streamed the response directly).
import threading
_local = threading.local()


def mark_ws_origin():
    """Call this before saving a message inside a WS consumer to suppress the signal push."""
    _local.ws_origin = True


def clear_ws_origin():
    """Call this after the save to reset the flag."""
    _local.ws_origin = False


def _is_ws_origin() -> bool:
    return getattr(_local, "ws_origin", False)


@receiver(post_save, sender="chat.ChatMessage")
def on_message_saved(sender, instance, created, **kwargs):
    """
    Push a new assistant message to the conversation's WS group.
    Skipped when the message was created inside a WS consumer (already streamed).
    """
    if not created:
        return
    if instance.role != "assistant":
        return
    if _is_ws_origin():
        # Already streamed by the consumer — don't double-push
        return

    try:
        from chat.notify import push_chat_chunk, push_chat_done
        conv_id = instance.conversation_id
        content = instance.content or ""
        push_chat_chunk(conv_id, content=content)
        push_chat_done(conv_id, message_id=instance.pk)
    except Exception:
        pass


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
    except Exception:
        pass
