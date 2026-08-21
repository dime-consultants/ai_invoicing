# chat/tasks.py

"""
Celery tasks for the chat app.

Chat turn pipeline
------------------

run_chat_turn_task
    1. Load conversation/message/user.
    2. Prepare ChatMessageAttachment -> UploadedFile.
    3. Queue pipeline_ingest_task for newly-created files.
    4. Replace itself with wait_for_files_ready_task.

wait_for_files_ready_task
    1. Check UploadedFile.parse_status.
    2. If files are still processing, retry later.
    3. If any file failed, stop the chat turn with an error.
    4. When every file is parsed, replace itself with
       run_chat_turn_after_files_ready_task.

run_chat_turn_after_files_ready_task
    1. Re-load conversation/message/user.
    2. Call ChatService.get_response().
    3. Run AIEngineService -> ToolService -> Grok.
    4. Save assistant message.
    5. Save generated attachments.
    6. Push completion event.

This guarantees:

    receive_file()
        ↓
    pipeline_ingest_task
        ↓
    extract_file_text_task
        ↓
    parse_status="parsed"
        ↓
    AI agent

The worker never sleeps while waiting for extraction.
"""

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestration limits
# ---------------------------------------------------------------------------

_CHAT_SOFT_LIMIT = 60 * 8
_CHAT_HARD_LIMIT = 60 * 10

# File readiness polling.
#
# This is a Celery retry, not time.sleep(), so no worker is held while
# waiting for extraction.
_FILE_READY_RETRY_SECONDS = 3
_FILE_READY_MAX_RETRIES = 400


# ---------------------------------------------------------------------------
# Initial chat task
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    soft_time_limit=60 * 2,
    time_limit=60 * 3,
)
def run_chat_turn_task(
    self,
    *,
    turn_id: str,
    conversation_id: int,
    user_message_id: int,
    user_id: int,
    workflow_id: int | None = None,
    conversation_history: list[dict] | None = None,
):
    """
    Prepare the chat turn and hand it over to the file-readiness stage.

    This task does NOT call the AI agent when files are present.

    Celery replace() is used so callers waiting on the original AsyncResult
    continue to receive the final task's result.
    """

    from django.contrib.auth import get_user_model
    from django.core.cache import cache

    from chat.models import (
        ChatConversation,
        ChatMessage,
        ChatMessageAttachment,
    )
    from chat.notify import push_chat_error
    from chat.services import ChatService

    User = get_user_model()

    try:

        user = User.objects.get(
            pk=user_id
        )

        conv = (
            ChatConversation.objects
            .get(
                pk=conversation_id,
                user=user,
            )
        )

        user_msg = (
            ChatMessage.objects
            .get(pk=user_message_id)
        )

        # ---------------------------------------------------------------
        # Cancellation before preparation.
        # ---------------------------------------------------------------

        if cache.get(
            f"chat_cancel_{turn_id}"
        ):

            push_chat_error(
                conversation_id,
                message=(
                    "Turn cancelled before "
                    "file processing started."
                ),
                turn_id=turn_id,
            )

            cache.delete(
                f"chat_cancel_{turn_id}"
            )

            return {
                "ok": False,
                "cancelled": True,
                "turn_id": turn_id,
            }

        # ---------------------------------------------------------------
        # Resolve current-turn attachments.
        # ---------------------------------------------------------------

        file_attachments = list(
            ChatMessageAttachment.objects
            .filter(
                message=user_msg
            )
        )

        # ---------------------------------------------------------------
        # Persist attachment -> UploadedFile.
        #
        # This does NOT extract anything.
        # ---------------------------------------------------------------

        batch, newly_created_ids = (
            ChatService.prepare_attachments(
                file_attachments,
                user,
            )
        )

        # ---------------------------------------------------------------
        # Queue the asynchronous file pipeline for files created by
        # this chat turn.
        #
        # pipeline_ingest_task then queues extract_file_text_task.
        # ---------------------------------------------------------------

        if newly_created_ids:

            from uploads.tasks import (
                pipeline_ingest_task
            )

            for file_id in newly_created_ids:

                pipeline_ingest_task.delay(
                    file_id
                )

            logger.info(
                "Chat turn %s queued ingestion "
                "for %s file(s): %s",
                turn_id,
                len(newly_created_ids),
                newly_created_ids,
            )

        # ---------------------------------------------------------------
        # If this turn has files, replace the current task with the
        # non-blocking readiness watcher.
        # ---------------------------------------------------------------

        if file_attachments:

            return self.replace(
                wait_for_files_ready_task.si(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    user_id=user_id,
                    workflow_id=workflow_id,
                    conversation_history=(
                        conversation_history
                        or []
                    ),
                )
            )

        # ---------------------------------------------------------------
        # No files.
        #
        # Skip the upload pipeline entirely and go directly to the
        # AI stage.
        # ---------------------------------------------------------------

        return self.replace(
            run_chat_turn_after_files_ready_task.si(
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                user_id=user_id,
                workflow_id=workflow_id,
                conversation_history=(
                    conversation_history
                    or []
                ),
            )
        )

    except SoftTimeLimitExceeded:
        raise

    except Exception as exc:

        logger.exception(
            "run_chat_turn_task preparation failed: "
            "turn=%s error=%s",
            turn_id,
            exc,
        )

        from chat.notify import push_chat_error

        push_chat_error(
            conversation_id,
            message=str(exc),
            turn_id=turn_id,
        )

        raise


# ---------------------------------------------------------------------------
# File readiness watcher
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=_FILE_READY_MAX_RETRIES,
    acks_late=True,
)
def wait_for_files_ready_task(
    self,
    *,
    turn_id: str,
    conversation_id: int,
    user_message_id: int,
    user_id: int,
    workflow_id: int | None = None,
    conversation_history: list[dict] | None = None,
):
    """
    Wait asynchronously until every file attached to the current chat
    message has completed extraction.

    This task NEVER calls time.sleep().

    While files are processing, Celery schedules a retry and the worker
    becomes available for other work.
    """

    from django.core.cache import cache

    from chat.models import ChatMessageAttachment
    from chat.notify import push_chat_error

    # ---------------------------------------------------------------
    # Cancellation.
    # ---------------------------------------------------------------

    if cache.get(
        f"chat_cancel_{turn_id}"
    ):

        cache.delete(
            f"chat_cancel_{turn_id}"
        )

        push_chat_error(
            conversation_id,
            message="Turn cancelled.",
            turn_id=turn_id,
        )

        return {
            "ok": False,
            "cancelled": True,
            "turn_id": turn_id,
        }

    # ---------------------------------------------------------------
    # Load files.
    # ---------------------------------------------------------------

    attachments = list(
        ChatMessageAttachment.objects
        .filter(
            message_id=user_message_id
        )
        .select_related(
            "uploaded_file"
        )
    )

    if not attachments:

        logger.info(
            "No attachments found for turn %s; "
            "continuing directly to AI.",
            turn_id,
        )

        return self.replace(
            run_chat_turn_after_files_ready_task.si(
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                user_id=user_id,
                workflow_id=workflow_id,
                conversation_history=(
                    conversation_history
                    or []
                ),
            )
        )

    # ---------------------------------------------------------------
    # Inspect readiness.
    # ---------------------------------------------------------------

    pending = []
    failed = []
    missing = []

    for attachment in attachments:

        uploaded_file = (
            attachment.uploaded_file
        )

        if not uploaded_file:

            missing.append(
                getattr(
                    attachment,
                    "filename",
                    "uploaded file",
                )
            )

            continue

        status = (
            uploaded_file.parse_status
            or "pending"
        )

        if status == "parsed":

            continue

        if status in (
            "error",
            "failed",
        ):

            failed.append(
                (
                    uploaded_file,
                    uploaded_file.parse_error
                    or
                    "File extraction failed.",
                )
            )

            continue

        pending.append(
            uploaded_file
        )

    # ---------------------------------------------------------------
    # Missing UploadedFile record.
    #
    # This should not happen after prepare_attachments(), but fail
    # explicitly rather than allowing the AI to run without the file.
    # ---------------------------------------------------------------

    if missing:

        names = ", ".join(
            missing
        )

        message = (
            "I could not prepare the following "
            f"file(s) for analysis: {names}"
        )

        push_chat_error(
            conversation_id,
            message=message,
            turn_id=turn_id,
        )

        return {
            "ok": False,
            "turn_id": turn_id,
            "error": message,
        }

    # ---------------------------------------------------------------
    # Extraction failure.
    # ---------------------------------------------------------------

    if failed:

        details = []

        for uploaded_file, error in failed:

            details.append(
                f"{uploaded_file.original_filename}: "
                f"{error}"
            )

        message = (
            "I couldn't finish processing the "
            "following file(s):\n\n"
            + "\n".join(details)
        )

        push_chat_error(
            conversation_id,
            message=message,
            turn_id=turn_id,
        )

        return {
            "ok": False,
            "turn_id": turn_id,
            "error": message,
        }

    # ---------------------------------------------------------------
    # Still processing.
    #
    # Celery retry releases the worker; it does NOT sleep here.
    # ---------------------------------------------------------------

    if pending:

        logger.info(
            "Chat turn %s waiting for %s file(s): %s "
            "(retry=%s/%s)",
            turn_id,
            len(pending),
            [
                file.pk
                for file in pending
            ],
            self.request.retries,
            self.max_retries,
        )

        raise self.retry(
            countdown=_FILE_READY_RETRY_SECONDS
        )

    # ---------------------------------------------------------------
    # All files are ready.
    #
    # ONLY NOW may the AI agent run.
    # ---------------------------------------------------------------

    logger.info(
        "All files ready for chat turn %s; "
        "starting AI agent.",
        turn_id,
    )

    return self.replace(
        run_chat_turn_after_files_ready_task.si(
            turn_id=turn_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            user_id=user_id,
            workflow_id=workflow_id,
            conversation_history=(
                conversation_history
                or []
            ),
        )
    )


# ---------------------------------------------------------------------------
# Actual AI chat execution
# ---------------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    soft_time_limit=_CHAT_SOFT_LIMIT,
    time_limit=_CHAT_HARD_LIMIT,
)
def run_chat_turn_after_files_ready_task(
    self,
    *,
    turn_id: str,
    conversation_id: int,
    user_message_id: int,
    user_id: int,
    workflow_id: int | None = None,
    conversation_history: list[dict] | None = None,
):
    """
    Run the actual AI chat turn.

    This task is intentionally impossible to reach from the normal file
    path until every UploadedFile has parse_status="parsed".
    """

    from django.contrib.auth import get_user_model
    from django.core.cache import cache

    from chat.models import (
        ChatConversation,
        ChatMessage,
        ChatMessageAttachment,
    )
    from chat.notify import (
        push_chat_chunk,
        push_chat_done,
        push_chat_error,
        push_chat_status,
    )
    from chat.services import ChatService
    from chat.views import _save_output_attachments

    User = get_user_model()

    # ---------------------------------------------------------------
    # Duplicate-delivery protection.
    #
    # The orchestration stages may be redelivered. Only one final AI
    # stage may create the assistant message.
    # ---------------------------------------------------------------

    lock_key = (
        f"chat_turn_lock_{turn_id}"
    )

    if not cache.add(
        lock_key,
        1,
        timeout=_CHAT_HARD_LIMIT + 60,
    ):

        logger.info(
            "run_chat_turn_after_files_ready_task: "
            "turn %s already claimed — skipping.",
            turn_id,
        )

        return {
            "ok": True,
            "skipped": "duplicate_delivery",
            "turn_id": turn_id,
        }

    def _cancelled() -> bool:
        return bool(
            cache.get(
                f"chat_cancel_{turn_id}"
            )
        )

    def _clear_cancel():
        cache.delete(
            f"chat_cancel_{turn_id}"
        )

    try:

        user = User.objects.get(
            pk=user_id
        )

        conv = (
            ChatConversation.objects
            .get(
                pk=conversation_id,
                user=user,
            )
        )

        user_msg = (
            ChatMessage.objects
            .get(
                pk=user_message_id
            )
        )

        content = user_msg.content

        # ---------------------------------------------------------------
        # Resolve workflow.
        # ---------------------------------------------------------------

        from chat.models import Workflow

        workflow = None

        if workflow_id:
            workflow = (
                Workflow.objects
                .filter(
                    pk=workflow_id
                )
                .first()
            )

        # ---------------------------------------------------------------
        # Cancellation before AI execution.
        # ---------------------------------------------------------------

        if _cancelled():

            push_chat_error(
                conversation_id,
                message=(
                    "Turn cancelled before "
                    "analysis started."
                ),
                turn_id=turn_id,
            )

            _clear_cancel()

            return {
                "ok": False,
                "cancelled": True,
                "turn_id": turn_id,
            }

        # ---------------------------------------------------------------
        # Resolve attachments.
        #
        # At this point the readiness task has already confirmed that
        # all UploadedFile rows are parsed.
        # ---------------------------------------------------------------

        file_attachments = list(
            ChatMessageAttachment.objects
            .filter(
                message=user_msg
            )
            .select_related(
                "uploaded_file"
            )
        )

        # ---------------------------------------------------------------
        # Final defensive readiness check.
        #
        # Never trust orchestration alone.
        # ---------------------------------------------------------------

        not_ready = []

        for attachment in file_attachments:

            uploaded_file = (
                attachment.uploaded_file
            )

            if not uploaded_file:

                not_ready.append(
                    getattr(
                        attachment,
                        "filename",
                        "uploaded file",
                    )
                )

                continue

            if (
                uploaded_file.parse_status
                != "parsed"
            ):

                not_ready.append(
                    getattr(
                        uploaded_file,
                        "original_filename",
                        getattr(
                            attachment,
                            "filename",
                            "uploaded file",
                        ),
                    )
                )

        if not_ready:

            names = ", ".join(
                not_ready
            )

            message = (
                "The following file(s) are not "
                f"ready for analysis: {names}"
            )

            push_chat_error(
                conversation_id,
                message=message,
                turn_id=turn_id,
            )

            return {
                "ok": False,
                "error": message,
                "turn_id": turn_id,
            }

        # ---------------------------------------------------------------
        # Status callback.
        # ---------------------------------------------------------------

        def _on_status_update(
            status,
            tool_name,
        ):

            push_chat_status(
                conversation_id,
                status=status,
                tool_name=tool_name,
                turn_id=turn_id,
            )

        # ---------------------------------------------------------------
        # Run AI.
        #
        # No extraction task is started here.
        # All files have already reached "parsed".
        # ---------------------------------------------------------------

        try:

            response_text, output_files = (
                ChatService.get_response(
                    message=content,
                    user=user,
                    file_attachments=(
                        file_attachments
                    ),
                    workflow_id=workflow_id,
                    conversation_history=(
                        conversation_history
                        or []
                    ),
                    conversation=conv,
                    on_status_update=(
                        _on_status_update
                    ),
                )
            )

        except SoftTimeLimitExceeded:

            push_chat_error(
                conversation_id,
                message=(
                    "Processing took too long "
                    "and was stopped. Try a "
                    "smaller file."
                ),
                turn_id=turn_id,
            )

            raise

        # ---------------------------------------------------------------
        # Cancellation after AI execution.
        # ---------------------------------------------------------------

        if _cancelled():

            _clear_cancel()

            push_chat_error(
                conversation_id,
                message="Turn cancelled.",
                turn_id=turn_id,
            )

            return {
                "ok": False,
                "cancelled": True,
                "turn_id": turn_id,
            }

        # ---------------------------------------------------------------
        # Push response progressively.
        # ---------------------------------------------------------------

        CHUNK = 400

        for i in range(
            0,
            len(response_text),
            CHUNK,
        ):

            push_chat_chunk(
                conversation_id,
                content=response_text[
                    i:i + CHUNK
                ],
                turn_id=turn_id,
            )

            if _cancelled():

                _clear_cancel()

                push_chat_error(
                    conversation_id,
                    message=(
                        "Turn cancelled "
                        "mid-stream."
                    ),
                    turn_id=turn_id,
                )

                return {
                    "ok": False,
                    "cancelled": True,
                    "turn_id": turn_id,
                }

        # ---------------------------------------------------------------
        # Save assistant message.
        # ---------------------------------------------------------------

        from chat.signals import (
            clear_ws_origin,
            mark_ws_origin,
        )

        mark_ws_origin()

        try:

            assistant_msg = (
                ChatMessage.objects.create(
                    conversation=conv,
                    role="assistant",
                    content=response_text,
                    applied_workflow=workflow,
                )
            )

        finally:

            clear_ws_origin()

        # ---------------------------------------------------------------
        # Persist output files.
        # ---------------------------------------------------------------

        saved_attachments = (
            _save_output_attachments(
                output_files,
                assistant_msg,
            )
        )

        attachment_meta = [
            {
                "id": att.pk,
                "filename": att.filename,
                "file_type": att.file_type,
                "download_url": (
                    f"/api/chat/attachments/"
                    f"{att.pk}/download/"
                ),
            }
            for att in saved_attachments
        ]

        # ---------------------------------------------------------------
        # Update conversation metadata.
        # ---------------------------------------------------------------

        from django.utils import timezone

        if conv.title in (
            "Untitled Conversation",
            "",
        ):

            from chat.title_generator import (
                generate_title_from_user_input
            )

            conv.title = (
                generate_title_from_user_input(
                    content
                )
                or content[:50]
            )

        conv.updated_at = timezone.now()

        conv.save(
            update_fields=[
                "title",
                "updated_at",
            ]
        )

        # ---------------------------------------------------------------
        # Complete.
        # ---------------------------------------------------------------

        push_chat_done(
            conversation_id,
            message_id=assistant_msg.pk,
            turn_id=turn_id,
            attachments=attachment_meta,
        )

        _clear_cancel()

        logger.info(
            "chat turn %s complete: "
            "conv=%s user=%s",
            turn_id,
            conversation_id,
            user_id,
        )

        return {
            "ok": True,
            "turn_id": turn_id,
            "assistant_message_id": (
                assistant_msg.pk
            ),
            "attachment_ids": [
                item["id"]
                for item in attachment_meta
            ],
        }

    except SoftTimeLimitExceeded:

        logger.error(
            "run_chat_turn_after_files_ready_task "
            "soft time limit exceeded: turn=%s",
            turn_id,
        )

        raise

    except Exception as exc:

        logger.exception(
            "run_chat_turn_after_files_ready_task "
            "failed: turn=%s error=%s",
            turn_id,
            exc,
        )

        from tools.services import (
            is_transient_llm_error
        )

        if (
            is_transient_llm_error(exc)
            and self.request.retries
            < self.max_retries
        ):

            # Release the duplicate-delivery lock so the retry can
            # claim the turn again.
            cache.delete(
                lock_key
            )

            raise self.retry(
                exc=exc,
                countdown=(
                    15
                    * (
                        self.request.retries
                        + 1
                    )
                ),
            )

        push_chat_error(
            conversation_id,
            message=str(exc),
            turn_id=turn_id,
        )

        raise