# chat/consumers.py
"""
WebSocket consumer for real-time chat.

Message protocol (client → server):
    {
        "action":      "send_message",
        "message":     "Extract all receipts and flag anomalies",
        "workflow_id": 3,          // optional — Workflow PK
        "batch_id":    12          // optional — UploadBatch PK to set as context
    }

Message protocol (server → client):
    {
        "type":       "message",
        "role":       "user" | "assistant" | "system",
        "content":    "...",
        "status":     "thinking" | "done" | "error",   // assistant only
        "ai_job_id":  42,          // set when an AI job was created
        "created_at": "2026-05-21T10:00:00Z"
    }
"""

import json
from datetime import datetime, timezone as tz

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class ChatConsumer(AsyncJsonWebsocketConsumer):

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_{self.conversation_id}"

        if not self.scope["user"].is_authenticated:
            await self.close(code=4003)
            return

        if not await self._user_can_access():
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self._send_history()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    # ── Receive ───────────────────────────────────────────────────────────────

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "content": "Invalid JSON."})
            return

        if data.get("action") != "send_message":
            return

        message = str(data.get("message", "")).strip()
        if not message:
            return

        workflow_id = data.get("workflow_id")   # optional int
        batch_id    = data.get("batch_id")      # optional int

        # 1. Persist the user message
        await self._save_message("user", message)

        # 2. Broadcast the user message to the group
        await self._broadcast("user", message, status="done")

        # 3. Signal "thinking…" to the client
        await self._broadcast("assistant", "", status="thinking")

        # 4. Delegate to the AI engine service (runs synchronously in a thread)
        try:
            response_text, ai_job_id = await sync_to_async(
                self._run_ai, thread_sensitive=True
            )(message, workflow_id=workflow_id, batch_id=batch_id)
        except Exception as exc:
            await self._broadcast(
                "assistant",
                f"Sorry, something went wrong: {exc}",
                status="error",
            )
            return

        # 5. Persist the assistant message
        await self._save_message("assistant", response_text, ai_job_id=ai_job_id)

        # 6. Broadcast the final assistant message
        await self._broadcast("assistant", response_text, status="done", ai_job_id=ai_job_id)

    # ── Group message handler ─────────────────────────────────────────────────

    async def chat_message(self, event):
        """Called by channel_layer.group_send — forwards to this WebSocket."""
        await self.send_json({
            "type":       "message",
            "role":       event["role"],
            "content":    event["content"],
            "status":     event.get("status", "done"),
            "ai_job_id":  event.get("ai_job_id"),
            "created_at": event.get("created_at"),
        })

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _broadcast(self, role: str, content: str, status: str = "done", ai_job_id=None):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type":       "chat.message",
                "role":       role,
                "content":    content,
                "status":     status,
                "ai_job_id":  ai_job_id,
                "created_at": datetime.now(tz.utc).isoformat(),
            },
        )

    def _run_ai(self, message: str, workflow_id=None, batch_id=None) -> tuple[str, int | None]:
        """
        Synchronous — runs in a thread via sync_to_async.

        Imports are deferred to avoid circular imports at module load.
        Returns (response_text, ai_job_id | None).
        """
        from ai_engine.services import AIEngineService
        from .models import ChatConversation

        conversation = ChatConversation.objects.get(pk=self.conversation_id)
        user = self.scope["user"]

        # Resolve batch: use conversation's pinned batch, or the one passed by client
        batch = conversation.related_batch
        if not batch and batch_id:
            from uploads.models import UploadBatch
            try:
                batch = UploadBatch.objects.get(pk=batch_id, uploaded_by=user)
                # Pin it to the conversation for subsequent messages
                conversation.related_batch = batch
                conversation.save(update_fields=["related_batch", "updated_at"])
            except UploadBatch.DoesNotExist:
                pass

        # Resolve workflow
        workflow = None
        if workflow_id:
            from .models import Workflow
            try:
                workflow = Workflow.objects.get(pk=workflow_id, enabled=True)
            except Workflow.DoesNotExist:
                pass

        # Build recent conversation history for context (last 20 turns)
        from .models import ChatMessage
        history = list(
            ChatMessage.objects.filter(conversation=conversation)
            .order_by("created_at")
            .values("role", "content")
        )[-20:]

        return AIEngineService.handle_chat_message(
            user=user,
            message=message,
            batch=batch,
            workflow=workflow,
            conversation_history=history,
        )

    async def _send_history(self):
        history = await self._get_history()
        for msg in history:
            await self.send_json({
                "type":       "message",
                "role":       msg["role"],
                "content":    msg["content"],
                "created_at": (
                    msg["created_at"].isoformat()
                    if hasattr(msg["created_at"], "isoformat")
                    else msg["created_at"]
                ),
            })

    @database_sync_to_async
    def _user_can_access(self) -> bool:
        from .models import ChatConversation
        return ChatConversation.objects.filter(
            pk=self.conversation_id,
            user=self.scope["user"],
        ).exists()

    @database_sync_to_async
    def _save_message(self, role: str, content: str, ai_job_id=None):
        from .models import ChatConversation, ChatMessage
        from ai_engine.models import AIAnalysisJob

        conversation = ChatConversation.objects.get(pk=self.conversation_id)
        kwargs = {"conversation": conversation, "role": role, "content": content}
        if ai_job_id:
            try:
                kwargs["ai_job"] = AIAnalysisJob.objects.get(pk=ai_job_id)
            except AIAnalysisJob.DoesNotExist:
                pass
        ChatMessage.objects.create(**kwargs)

    @database_sync_to_async
    def _get_history(self):
        from .models import ChatMessage
        return list(
            ChatMessage.objects.filter(conversation_id=self.conversation_id)
            .order_by("created_at")
            .values("role", "content", "created_at")
        )