# chat/consumers.py
"""
WebSocket consumers for the K+N Finance Automation backend.

WS /ws/notifications/
    Real-time notifications pushed to the authenticated user.
    Each user has their own group: "notifications_<user_id>"
    The backend pushes to this group whenever a relevant event occurs
    (file parsed, AI job done, report ready, etc.).

WS /ws/chat/<conversation_id>/
    Real-time streaming of AI assistant responses for a conversation.
    Messages sent by the client are processed and the AI response is
    streamed back token-by-token (or in chunks).
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _notifications_group(user_id) -> str:
    return f"notifications_{user_id}"


def _chat_group(conversation_id) -> str:
    return f"chat_{conversation_id}"


# ─────────────────────────────────────────────────────────────────────────────
# WS /ws/notifications/
# ─────────────────────────────────────────────────────────────────────────────

class NotificationsConsumer(AsyncWebsocketConsumer):
    """
    Per-user notification channel.

    Connect:  ws://host/ws/notifications/?token=<jwt>
    Receive:  { "type": "ping" }  → responds with { "type": "pong" }
    Send:     { "type": "notification", "data": { id, title, message, timestamp } }

    The backend pushes notifications by calling:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"notifications_{user_id}",
            {
                "type":    "push_notification",
                "id":      str(uuid),
                "title":   "...",
                "message": "...",
                "timestamp": timezone.now().isoformat(),
            }
        )
    """

    async def connect(self):
        user = self.scope.get("user")

        if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
            logger.warning("WS /ws/notifications/ — unauthenticated, closing.")
            await self.close(code=4001)
            return

        self.user       = user
        self.group_name = _notifications_group(user.pk)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send a welcome message so the client knows the connection is live
        await self.send(text_data=json.dumps({
            "type":    "connected",
            "message": f"Notifications channel open for user {user.username}.",
        }))
        logger.info("WS notifications connected: user=%s group=%s", user.username, self.group_name)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info("WS notifications disconnected: code=%s", close_code)

    async def receive(self, text_data=None, bytes_data=None):
        """Handle messages from the client (e.g. ping/ack)."""
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

        elif msg_type == "ack":
            # Client acknowledging receipt of a notification — no-op for now
            pass

    # ── Group message handlers ────────────────────────────────────────────────

    async def push_notification(self, event):
        """
        Called when the backend sends a notification to this user's group.
        Forwards it to the WebSocket client in the contract shape.
        """
        await self.send(text_data=json.dumps({
            "type": "notification",
            "data": {
                "id":        event.get("id", ""),
                "title":     event.get("title", ""),
                "message":   event.get("message", ""),
                "timestamp": event.get("timestamp", ""),
            },
        }))

    async def job_update(self, event):
        """Push an AI job status update."""
        await self.send(text_data=json.dumps({
            "type": "job_update",
            "data": {
                "jobId":  event.get("job_id"),
                "status": event.get("status"),
                "title":  event.get("title", ""),
            },
        }))

    async def file_processed(self, event):
        """Push a file-processed notification."""
        await self.send(text_data=json.dumps({
            "type": "file_processed",
            "data": {
                "fileId":   event.get("file_id"),
                "filename": event.get("filename", ""),
                "status":   event.get("status", "parsed"),
            },
        }))


# ─────────────────────────────────────────────────────────────────────────────
# WS /ws/chat/<conversation_id>/
# ─────────────────────────────────────────────────────────────────────────────

class ChatStreamConsumer(AsyncWebsocketConsumer):
    """
    Real-time chat streaming for a specific conversation.

    Connect:  ws://host/ws/chat/<conversation_id>/?token=<jwt>

    Client → server:
        { "type": "message", "content": "user text", "workflow_id": null }

    Server → client (streaming):
        { "type": "text",  "content": "<chunk>" }   (one or more)
        { "type": "done",  "conversationId": "<id>", "messageId": "<id>" }

    Server → client (error):
        { "type": "error", "message": "..." }

    The consumer also joins the conversation group so other processes
    (e.g. a Celery worker finishing an AI job) can push updates:
        channel_layer.group_send(f"chat_{conversation_id}", { "type": "stream_chunk", ... })
    """

    async def connect(self):
        user = self.scope.get("user")

        if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
            logger.warning("WS /ws/chat/ — unauthenticated, closing.")
            await self.close(code=4001)
            return

        self.user            = user
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.group_name      = _chat_group(self.conversation_id)

        # Verify the conversation belongs to this user
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _check_ownership():
            from chat.models import ChatConversation
            return ChatConversation.objects.filter(
                pk=self.conversation_id, user=user
            ).exists()

        if not await _check_ownership():
            logger.warning(
                "WS /ws/chat/%s — user %s does not own this conversation.",
                self.conversation_id, user.username,
            )
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self.send(text_data=json.dumps({
            "type":           "connected",
            "conversationId": str(self.conversation_id),
        }))
        logger.info(
            "WS chat connected: user=%s conversation=%s",
            user.username, self.conversation_id,
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info("WS chat disconnected: conversation=%s code=%s",
                    getattr(self, "conversation_id", "?"), close_code)

    async def receive(self, text_data=None, bytes_data=None):
        """Handle a message from the client and stream the AI response back."""
        if not text_data:
            return

        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                "type":    "error",
                "message": "Invalid JSON.",
            }))
            return

        msg_type = data.get("type", "message")

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if msg_type != "message":
            return

        content     = (data.get("content") or "").strip()
        workflow_id = data.get("workflow_id")

        if not content:
            await self.send(text_data=json.dumps({
                "type":    "error",
                "message": "content is required.",
            }))
            return

        # Run the AI response in a thread (ChatService is synchronous)
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _get_ai_response():
            from chat.models import ChatConversation, ChatMessage
            from chat.services import ChatService

            # Load conversation history (last 20 turns)
            try:
                conv = ChatConversation.objects.get(
                    pk=self.conversation_id, user=self.user
                )
            except ChatConversation.DoesNotExist:
                return None, None, "Conversation not found."

            history = list(
                ChatMessage.objects.filter(conversation=conv)
                .order_by("created_at")
                .values("role", "content")
            )[-20:]
            conv_history = [{"role": m["role"], "content": m["content"]} for m in history]

            # Save user message
            user_msg = ChatMessage.objects.create(
                conversation=conv,
                role="user",
                content=content,
            )

            # Get AI response
            try:
                response_text, _ = ChatService.get_response(
                    message=content,
                    user=self.user,
                    workflow_id=int(workflow_id) if workflow_id else None,
                    conversation_history=conv_history,
                )
            except Exception as exc:
                logger.exception("ChatService failed in WS consumer: %s", exc)
                return None, user_msg.pk, str(exc)

            # Save assistant message — mark as WS-origin so the post_save
            # signal doesn't push a second time (we stream directly below).
            from chat.signals import mark_ws_origin, clear_ws_origin
            mark_ws_origin()
            try:
                assistant_msg = ChatMessage.objects.create(
                    conversation=conv,
                    role="assistant",
                    content=response_text,
                )
            finally:
                clear_ws_origin()

            # Auto-title
            from django.utils import timezone as tz
            if conv.title in ("Untitled Conversation", ""):
                conv.title = content[:50]
            conv.updated_at = tz.now()
            conv.save(update_fields=["title", "updated_at"])

            return response_text, assistant_msg.pk, None

        response_text, message_id, error = await _get_ai_response()

        if error:
            await self.send(text_data=json.dumps({
                "type":    "error",
                "message": error,
            }))
            return

        # Stream the response in chunks (simulate token streaming)
        # In production, replace with actual streaming from the LLM API
        chunk_size = 80
        for i in range(0, len(response_text), chunk_size):
            chunk = response_text[i: i + chunk_size]
            await self.send(text_data=json.dumps({
                "type":    "text",
                "content": chunk,
            }))

        # Signal completion
        await self.send(text_data=json.dumps({
            "type":           "done",
            "conversationId": str(self.conversation_id),
            "messageId":      str(message_id),
        }))

    # ── Group message handlers (pushed from other processes) ──────────────────

    async def stream_chunk(self, event):
        """Forward a streamed chunk from a background worker."""
        await self.send(text_data=json.dumps({
            "type":    "text",
            "content": event.get("content", ""),
        }))

    async def stream_done(self, event):
        """Signal that streaming is complete."""
        await self.send(text_data=json.dumps({
            "type":           "done",
            "conversationId": str(self.conversation_id),
            "messageId":      event.get("message_id", ""),
        }))

    async def stream_error(self, event):
        """Forward an error from a background worker."""
        await self.send(text_data=json.dumps({
            "type":    "error",
            "message": event.get("message", "An error occurred."),
        }))
