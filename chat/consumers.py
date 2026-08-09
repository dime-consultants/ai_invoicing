# chat/consumers.py
"""
WebSocket consumers for the ai_invoicing backend.

WS /ws/notifications/
    Per-user notification channel. The backend pushes to this group whenever
    a relevant event occurs (file parsed, AI job done, report ready, etc.).
    Group name: "notifications_<user_id>"

WS /ws/chat/<conversation_id>/
    Real-time chat for a specific conversation.

    Key design decisions vs. the old implementation
    ------------------------------------------------
    1. NO fake streaming — the consumer dispatches the turn to a Celery task
       (`run_chat_turn_task`) and returns immediately with a "processing" ack.
       The Celery worker calls push_chat_chunk / push_chat_done on the channel
       group as the LLM produces output, so the client sees real progress.

    2. NO synchronous heavy work on the ASGI event loop — DB writes and Grok
       calls happen in the Celery worker process, not inside database_sync_to_async
       on the WS thread.

    3. Cancellation — the client can send {"type": "cancel"} and the worker
       will stop processing (checked via a Redis flag).

    The consumers themselves are now very thin: authenticate, validate
    ownership, ack the message, fire the task, done.
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)


def _notifications_group(user_id: int) -> str:
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
    Client→server: { "type": "ping" }
    Server→client: { "type": "notification" | "job_update" | "file_processed", "data": {...} }
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
        await self.send(text_data=json.dumps({
            "type":    "connected",
            "message": f"Notifications open for {user.username}.",
        }))
        logger.info("WS notifications connected: user=%s", user.username)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if data.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    # ── Group message handlers ────────────────────────────────────────────────

    async def push_notification(self, event):
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
        await self.send(text_data=json.dumps({
            "type": "job_update",
            "data": {
                "jobId":  event.get("job_id"),
                "status": event.get("status"),
                "title":  event.get("title", ""),
            },
        }))

    async def file_processed(self, event):
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
        { "type": "cancel" }   — cancel the in-progress turn
        { "type": "ping"   }

    Server → client:
        { "type": "ack",         "turnId": "<uuid>" }          immediately after message received
        { "type": "processing",  "turnId": "<uuid>" }          Celery task accepted
        { "type": "text",        "content": "<chunk>",
                                 "turnId": "<uuid>" }          streamed from worker
        { "type": "done",        "conversationId": "...",
                                 "messageId": "...",
                                 "turnId":    "<uuid>",
                                 "attachments": [...] }        final
        { "type": "error",       "message": "...",
                                 "turnId": "<uuid>" }
        { "type": "cancelled",   "turnId": "<uuid>" }

    The consumer is intentionally thin — it only:
      1. Authenticates the user and verifies conversation ownership.
      2. Saves the user message to the DB.
      3. Fires run_chat_turn_task.delay(turn_id, ...) and acks the client.
      4. Forwards group messages from the worker back to the client.
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
        self._active_turn_id = None

        # Verify ownership without blocking the event loop too long
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _check():
            from chat.models import ChatConversation
            return ChatConversation.objects.filter(
                pk=self.conversation_id, user=user
            ).exists()

        if not await _check():
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
        logger.info("WS chat connected: user=%s conversation=%s",
                    user.username, self.conversation_id)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info("WS chat disconnected: conversation=%s code=%s",
                    getattr(self, "conversation_id", "?"), close_code)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON.", turn_id=None)
            return

        msg_type = data.get("type", "message")

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if msg_type == "cancel":
            await self._handle_cancel()
            return

        if msg_type != "message":
            return

        content     = (data.get("content") or "").strip()
        workflow_id = data.get("workflow_id")

        if not content:
            await self._send_error("content is required.", turn_id=None)
            return

        import uuid
        turn_id = str(uuid.uuid4())
        self._active_turn_id = turn_id

        # Ack immediately so the client can render the user bubble
        await self.send(text_data=json.dumps({"type": "ack", "turnId": turn_id}))

        # Save user message and fire Celery task — all in one sync call
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _save_and_dispatch():
            from chat.models import ChatConversation, ChatMessage
            from chat.tasks import run_chat_turn_task

            try:
                conv = ChatConversation.objects.get(
                    pk=self.conversation_id, user=self.user
                )
            except ChatConversation.DoesNotExist:
                return False, "Conversation not found."

            # Build history snapshot before saving the new message
            history = list(
                ChatMessage.objects
                .filter(conversation=conv)
                .exclude(role="system")
                .order_by("-created_at")
                .values("role", "content")[:20]
            )
            history.reverse()
            conv_history = [{"role": m["role"], "content": m["content"]} for m in history]

            user_msg = ChatMessage.objects.create(
                conversation=conv,
                role="user",
                content=content,
            )

            # Dispatch to Celery — worker will push chunks to the group
            from config.dispatch import dispatch
            queued = dispatch(
                run_chat_turn_task,
                turn_id=turn_id,
                conversation_id=self.conversation_id,
                user_message_id=user_msg.pk,
                user_id=self.user.pk,
                workflow_id=int(workflow_id) if workflow_id else None,
                conversation_history=conv_history,
            )
            if queued is None:
                return False, ("Could not queue the message — the task broker "
                               "is unavailable. Please retry.")
            return True, None

        ok, err = await _save_and_dispatch()
        if not ok:
            await self._send_error(err, turn_id=turn_id)
            return

        await self.send(text_data=json.dumps({
            "type":    "processing",
            "turnId":  turn_id,
            "message": "Your message is being processed.",
        }))

    # ── Cancel ────────────────────────────────────────────────────────────────

    async def _handle_cancel(self):
        turn_id = self._active_turn_id
        if not turn_id:
            return
        # Set a Redis flag the worker checks between tool calls
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _set_cancel():
            from django.core.cache import cache
            cache.set(f"chat_cancel_{turn_id}", True, timeout=300)

        await _set_cancel()
        self._active_turn_id = None
        await self.send(text_data=json.dumps({
            "type":    "cancelled",
            "turnId":  turn_id,
        }))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send_error(self, message: str, turn_id):
        payload = {"type": "error", "message": message}
        if turn_id:
            payload["turnId"] = turn_id
        await self.send(text_data=json.dumps(payload))

    # ── Group message handlers (pushed from Celery worker) ────────────────────

    async def stream_chunk(self, event):
        await self.send(text_data=json.dumps({
            "type":    "text",
            "content": event.get("content", ""),
            "turnId":  event.get("turn_id", ""),
        }))

    async def stream_done(self, event):
        self._active_turn_id = None
        await self.send(text_data=json.dumps({
            "type":           "done",
            "conversationId": str(self.conversation_id),
            "messageId":      event.get("message_id", ""),
            "turnId":         event.get("turn_id", ""),
            "attachments":    event.get("attachments", []),
        }))

    async def stream_error(self, event):
        self._active_turn_id = None
        await self.send(text_data=json.dumps({
            "type":    "error",
            "message": event.get("message", "An error occurred."),
            "turnId":  event.get("turn_id", ""),
        }))