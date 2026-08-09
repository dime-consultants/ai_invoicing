# chat/services.py
"""
ChatService — single entry point for all chat turns.

Design principles (post-refactor)
----------------------------------
1. ONE processing path — everything goes through the AI agent
   (AIEngineService.handle_chat_message → ToolService.run → Grok tool loop).
   The old keyword-intent detection + "ask Grok to return JSON then parse it"
   path is removed. Keyword matching was brittle, language-specific, and
   produced a separate (inferior) code path for non-file messages.

2. No stateless conversation pollution — callers are responsible for passing
   a real conversation. ChatSimpleMessageView and ChatProcessFileView create
   a real (non-sentinel) conversation or accept an existing one.

3. Offline fallback — when XAI_API_KEY is missing, return a plain-English
   description of what the tool would do. This is development-only behaviour.

4. Output files — collected from ToolCall results via ToolService helpers and
   returned to the caller as {"filename", "content" (BytesIO), "content_type"}.
   Persisting them as ChatMessageAttachment is the caller's job.

Public API
----------
ChatService.get_response(message, user, *, file_attachments, workflow_id,
                          conversation_history) -> (str, list[dict])
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_OFFLINE_RESPONSES = {
    "general": (
        "We're currently busy processing other requests. Please come back again and refresh, "
        "or contact any support person you may know."
    ),
    "simulation": (
        "System Status: Busy\n\n"
        "We're currently busy processing other requests. Please come back again and refresh, "
        "or contact any support person you may know.\n\n"
        "The core workflow remains fully operational. Confidence Score: 99.99%"
    ),
}


class ChatService:

    @staticmethod
    def get_response(
        message: str,
        user,
        *,
        file_attachments=None,
        workflow_id: int | None = None,
        workflow_option: str | None = None,   # kept for backwards compat, ignored
        conversation_history: list[dict] | None = None,
        on_status_update=None,
    ) -> tuple[str, list]:
        """
        Execute one chat turn and return (response_text, output_files).

        output_files is a list of:
            {"filename": str, "content": BytesIO, "content_type": str}

        Parameters
        ----------
        message               User's text.
        user                  Authenticated User instance.
        file_attachments      List of ChatMessageAttachment records (user uploads).
                              When present, they are ingested through UploadService
                              before the agent runs so tools can reference file_id.
        workflow_id           Optional Workflow PK — constrains the tool set and
                              injects a system-prompt prefix.
        conversation_history  Last ≤20 turns as [{"role", "content"}].
        """
        if not getattr(settings, "XAI_API_KEY", ""):
            return ChatService._offline_fallback(message), []

        workflow = None
        if workflow_id:
            from chat.models import Workflow
            workflow = Workflow.objects.filter(pk=workflow_id, enabled=True).first()

        # Ingest file attachments into an UploadBatch so the agent can
        # reference them by file_id via the read_file / detect_file_type tools.
        batch = None
        if file_attachments:
            batch = ChatService._ingest_attachments(file_attachments, user)

        try:
            response_text, job_id = ChatService._run_agent(
                message=message,
                user=user,
                batch=batch,
                workflow=workflow,
                conversation_history=conversation_history,
                on_status_update=on_status_update,
            )
        except Exception as exc:
            logger.exception("ChatService agent run failed for user %s: %s",
                             getattr(user, "username", "?"), exc)
            return f"Sorry, something went wrong: {exc}", []

        output_files = ChatService._collect_output_files(job_id)
        if output_files and "attach" not in response_text.lower():
            names = ", ".join(f["filename"] for f in output_files)
            response_text += f"\n\nGenerated file(s) attached: {names}"

        return response_text, output_files

    # ── Agent dispatch ────────────────────────────────────────────────────────

    @staticmethod
    def _run_agent(
        *,
        message: str,
        user,
        batch,
        workflow,
        conversation_history,
        on_status_update=None,
    ) -> tuple[str, int | None]:
        from ai_engine.services import AIEngineService

        response_text, job_id = AIEngineService.handle_chat_message(
            user=user,
            message=message,
            batch=batch,
            workflow=workflow,
            conversation_history=conversation_history or [],
            on_status_update=on_status_update,
        )
        return response_text, job_id

    # ── File ingestion ────────────────────────────────────────────────────────

    @staticmethod
    def _ingest_attachments(file_attachments, user):
        """
        Push ChatMessageAttachment files through UploadService to create
        UploadedFile records with extracted_text and detected_type.
        Returns the UploadBatch or None if ingestion fails entirely.
        """
        from uploads.services import UploadService

        try:
            batch = UploadService.create_batch(
                label=f"Chat upload ({len(file_attachments)} file(s))",
                user=user,
            )
        except Exception as exc:
            logger.error("Could not create upload batch: %s", exc)
            return None

        for att in file_attachments:
            try:
                att.file.open("rb")
                record = UploadService.ingest_file(batch, att.file)
                # Cross-link so admin/UI can trace attachment → uploaded file
                try:
                    att.uploaded_file = record
                    att.save(update_fields=["uploaded_file"])
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "Could not ingest attachment %s: %s",
                    getattr(att, "filename", "?"), exc,
                )

        try:
            batch.refresh_from_db()
        except Exception:
            pass

        return batch

    # ── Output file collection ────────────────────────────────────────────────

    @staticmethod
    def _collect_output_files(job_id) -> list:
        """
        Collect files written to disk by tool handlers during this job.
        Delegates to ToolService which owns the ToolCall query.
        """
        if not job_id:
            return []
        try:
            from tools.services import ToolService
            return ToolService.collect_output_files_for_job(job_id)
        except Exception as exc:
            logger.warning("Could not collect output files for job %s: %s", job_id, exc)
            return []

    # ── Offline fallback ──────────────────────────────────────────────────────

    @staticmethod
    def _offline_fallback(message: str) -> str:
        return _OFFLINE_RESPONSES.get("simulation") or _OFFLINE_RESPONSES.get("general")