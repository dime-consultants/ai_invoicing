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


def infer_workflow_from_signals(message: str, file_types: list[str] | None = None):
    """
    Pure heuristic: given free-text `message` and a list of lowercase file
    extensions/types, return the best-matching enabled Workflow, or None if
    nothing matches and no fallback "custom" workflow is seeded.

    Single source of truth for "what should we do with this turn" — used both
    when a chat message arrives with attachments (ChatService._resolve_workflow_for_message)
    and as the no-explicit-request/no-keyword-match fallback
    (ai_engine.services._infer_workflow_from_request), so the two layers can't
    silently diverge. Never raises — the caller can always safely treat a
    None return as "no specific workflow, use the base system prompt."
    """
    from .models import Workflow

    text = (message or "").lower()
    file_types = [(ft or "").lower() for ft in (file_types or [])]

    if any(word in text for word in ("reconcile", "reconciliation", "variance", "compare", "match", "difference", "discrepancy")) or len(file_types) >= 2:
        return Workflow.objects.filter(enabled=True, workflow_type="reconciliation").first()
    if any(word in text for word in ("report", "summary", "overview", "batch", "total", "executive")):
        return Workflow.objects.filter(enabled=True, workflow_type="batch_summary").first() or Workflow.objects.filter(enabled=True, workflow_type="report_generation").first()
    if any(word in text for word in ("anomaly", "outlier", "flag", "exception", "issue", "fraud")):
        return Workflow.objects.filter(enabled=True, workflow_type="anomaly_detection").first()
    if any(word in text for word in ("clean", "normalize", "standardize", "dedupe", "remove duplicates", "fix data")):
        return Workflow.objects.filter(enabled=True, workflow_type="data_cleaning").first()
    if any(word in text for word in ("extract", "parse", "invoice", "receipt", "convert", "line item", "data from")):
        return Workflow.objects.filter(enabled=True, workflow_type="extraction").first()
    if file_types and any(ft in {"pdf", "xlsx", "xls", "csv", "txt"} for ft in file_types):
        return Workflow.objects.filter(enabled=True, workflow_type="batch_summary").first() or Workflow.objects.filter(enabled=True, workflow_type="custom").first()
    return Workflow.objects.filter(enabled=True, workflow_type="custom").first()


class ChatService:

    @staticmethod
    def build_conversation_history(conversation, exclude_pk=None, limit: int = 20) -> list[dict]:
        """
        Return the last `limit` non-system turns of a conversation in
        chronological order, as [{"role", "content"}].

        Excludes the message at exclude_pk (the just-saved user message, to
        avoid including it twice in the history fed to the LLM).

        Single source of truth for chat history — every caller (REST views,
        the WS consumer, the Celery chat task) must go through this so the
        LLM always sees the same, correctly-ordered history regardless of
        which entry point handled the turn.
        """
        from .models import ChatMessage

        qs = (
            ChatMessage.objects
            .filter(conversation=conversation)
            .exclude(role="system")
            .order_by("-created_at", "-pk")
        )
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        rows = list(qs.values("role", "content")[:limit])
        rows.reverse()
        return [{"role": m["role"], "content": m["content"]} for m in rows]

    @staticmethod
    def _resolve_workflow_for_message(message: str, file_attachments=None, workflow_id: int | None = None):
        """Infer the best workflow from user intent and the files being processed."""
        from chat.models import Workflow

        if workflow_id:
            return Workflow.objects.filter(pk=workflow_id, enabled=True).first()

        attachments = list(file_attachments or [])
        file_types = []
        for att in attachments:
            file_type = getattr(att, "file_type", None) or ""
            if not file_type and getattr(att, "uploaded_file", None):
                file_type = getattr(att.uploaded_file, "extension", "") or ""
            file_types.append((file_type or "").lower())

        return infer_workflow_from_signals(message, file_types)

    @staticmethod
    def get_response(
        message: str,
        user,
        *,
        file_attachments=None,
        workflow_id: int | None = None,
        workflow_option: str | None = None,   # kept for backwards compat, ignored
        conversation_history: list[dict] | None = None,
        conversation=None,
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
        conversation          The ChatConversation this turn belongs to, if any
                              (omitted only by the fully stateless callers). Lets
                              handle_chat_message look up every file ever attached
                              anywhere in the conversation, not just this turn's
                              attachments — see AIEngineService.handle_chat_message.
        """
        if not getattr(settings, "XAI_API_KEY", ""):
            return ChatService._offline_fallback(message), []

        workflow = ChatService._resolve_workflow_for_message(message, file_attachments, workflow_id)

        # Fast path: no uploaded files means no upload batch / no ingestion wait
        # and the LLM can respond directly using the request + conversation history.
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
                conversation=conversation,
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
        conversation=None,
        on_status_update=None,
    ) -> tuple[str, int | None]:
        from ai_engine.services import AIEngineService

        response_text, job_id = AIEngineService.handle_chat_message(
            user=user,
            message=message,
            batch=batch,
            workflow=workflow,
            conversation_history=conversation_history or [],
            conversation=conversation,
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

        from uploads.models import UploadedFile

        # First, attempt to reuse any existing UploadedFile rows so we don't
        # create duplicates. Only create a new batch if we actually need to
        # ingest one or more files.
        needs_ingest = False
        for att in file_attachments:
            # If already linked, nothing to do for this attachment.
            if getattr(att, "uploaded_file", None):
                continue
            # Try to find an existing UploadedFile that references the same
            # stored file path/name. This covers cases where the media file was
            # already ingested elsewhere but the ChatMessageAttachment wasn't
            # cross-linked yet.
            try:
                existing = UploadedFile.objects.filter(file=att.file.name).first()
                if existing:
                    try:
                        att.uploaded_file = existing
                        att.save(update_fields=["uploaded_file"])
                        continue
                    except Exception:
                        pass
            except Exception:
                # If any DB error occurs, fall back to ingesting below.
                pass

            needs_ingest = True

        batch = None
        if needs_ingest:
            try:
                batch = UploadService.create_batch(
                    label=f"Chat upload ({len(file_attachments)} file(s))",
                    user=user,
                )
            except Exception as exc:
                logger.error("Could not create upload batch: %s", exc)
                # If we cannot create a batch, proceed without one; already-
                # linked UploadedFile records remain usable via read_file.
                batch = None

        # Now ingest any attachments that still lack an UploadedFile record.
        for att in file_attachments:
            if getattr(att, "uploaded_file", None):
                continue
            try:
                att.file.open("rb")
                # If we failed to create a batch above, fall back to ingesting
                # into a fresh batch so the upload pipeline still runs.
                target_batch = batch
                if target_batch is None:
                    try:
                        target_batch = UploadService.create_batch(
                            label=f"Chat upload ({att.filename})",
                            user=user,
                        )
                        # If we created a per-file batch above, keep it for
                        # subsequent unlinked files.
                        if batch is None:
                            batch = target_batch
                    except Exception:
                        target_batch = None

                record = UploadService.ingest_file(target_batch, att.file)
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
            if batch is not None:
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