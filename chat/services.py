# chat/services.py

"""
ChatService — single entry point for AI-backed chat turns.

Architecture
------------

File uploads are prepared by chat/tasks.py before this service is called.

The runtime pipeline is:

    ChatMessageAttachment
        ↓
    UploadedFile
        ↓
    pipeline_ingest_task
        ↓
    extract_file_text_task
        ↓
    parse_status == "parsed"
        ↓
    ChatService.get_response()
        ↓
    AIEngineService.handle_chat_message()
        ↓
    ToolService.run()
        ↓
    Grok

ChatService itself does not wait for Celery extraction.

This prevents the AI agent from seeing an UploadedFile before its
extracted content is ready.
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


def infer_workflow_from_signals(
    message: str,
    file_types: list[str] | None = None,
):
    """
    Pure heuristic for workflow selection.

    Never raises.
    """

    from .models import Workflow

    text = (message or "").lower()
    file_types = [
        (ft or "").lower()
        for ft in (file_types or [])
    ]

    if (
        any(
            word in text
            for word in (
                "reconcile",
                "reconciliation",
                "variance",
                "compare",
                "match",
                "difference",
                "discrepancy",
            )
        )
        or len(file_types) >= 2
    ):
        return Workflow.objects.filter(
            enabled=True,
            workflow_type="reconciliation",
        ).first()

    if any(
        word in text
        for word in (
            "report",
            "summary",
            "overview",
            "batch",
            "total",
            "executive",
        )
    ):
        return (
            Workflow.objects.filter(
                enabled=True,
                workflow_type="batch_summary",
            ).first()
            or
            Workflow.objects.filter(
                enabled=True,
                workflow_type="report_generation",
            ).first()
        )

    if any(
        word in text
        for word in (
            "anomaly",
            "outlier",
            "flag",
            "exception",
            "issue",
            "fraud",
        )
    ):
        return Workflow.objects.filter(
            enabled=True,
            workflow_type="anomaly_detection",
        ).first()

    if any(
        word in text
        for word in (
            "clean",
            "normalize",
            "standardize",
            "dedupe",
            "remove duplicates",
            "fix data",
        )
    ):
        return Workflow.objects.filter(
            enabled=True,
            workflow_type="data_cleaning",
        ).first()

    if any(
        word in text
        for word in (
            "extract",
            "parse",
            "invoice",
            "receipt",
            "convert",
            "line item",
            "data from",
        )
    ):
        return Workflow.objects.filter(
            enabled=True,
            workflow_type="extraction",
        ).first()

    if file_types and any(
        ft in {
            "pdf",
            "xlsx",
            "xls",
            "csv",
            "txt",
        }
        for ft in file_types
    ):
        return (
            Workflow.objects.filter(
                enabled=True,
                workflow_type="batch_summary",
            ).first()
            or
            Workflow.objects.filter(
                enabled=True,
                workflow_type="custom",
            ).first()
        )

    return Workflow.objects.filter(
        enabled=True,
        workflow_type="custom",
    ).first()


class ChatService:

    @staticmethod
    def build_conversation_history(
        conversation,
        exclude_pk=None,
        limit: int = 20,
    ) -> list[dict]:
        """
        Return the last `limit` non-system turns in chronological order.
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

        rows = list(
            qs.values(
                "role",
                "content",
            )[:limit]
        )

        rows.reverse()

        return [
            {
                "role": row["role"],
                "content": row["content"],
            }
            for row in rows
        ]

    @staticmethod
    def _resolve_workflow_for_message(
        message: str,
        file_attachments=None,
        workflow_id: int | None = None,
    ):
        """
        Resolve workflow from explicit selection, user intent and
        attached file types.
        """

        from chat.models import Workflow

        if workflow_id:
            return Workflow.objects.filter(
                pk=workflow_id,
                enabled=True,
            ).first()

        attachments = list(
            file_attachments or []
        )

        file_types = []

        for att in attachments:

            file_type = (
                getattr(att, "file_type", None)
                or ""
            )

            uploaded_file = getattr(
                att,
                "uploaded_file",
                None,
            )

            if (
                not file_type
                and uploaded_file
            ):
                file_type = (
                    getattr(
                        uploaded_file,
                        "extension",
                        "",
                    )
                    or ""
                )

            file_types.append(
                file_type.lower()
            )

        return infer_workflow_from_signals(
            message,
            file_types,
        )

    @staticmethod
    def prepare_attachments(
        file_attachments,
        user,
    ) -> tuple[object | None, list[int]]:
        """
        Persist chat attachments as UploadedFile records.

        IMPORTANT
        ---------
        This method does NOT start the extraction pipeline.

        It only performs:

            ChatMessageAttachment
                ↓
            UploadedFile

        The caller is responsible for queueing:

            pipeline_ingest_task
                ↓
            extract_file_text_task

        Returns:
            (batch, newly_created_uploaded_file_ids)
        """

        from uploads.models import UploadedFile
        from uploads.services import UploadService

        attachments = list(
            file_attachments or []
        )

        if not attachments:
            return None, []

        needs_ingest = []

        for att in attachments:

            if getattr(
                att,
                "uploaded_file",
                None,
            ):
                continue

            try:
                existing = (
                    UploadedFile.objects
                    .filter(
                        file=att.file.name
                    )
                    .first()
                )

                if existing:

                    try:
                        att.uploaded_file = existing

                        att.save(
                            update_fields=[
                                "uploaded_file"
                            ]
                        )

                        continue

                    except Exception:
                        logger.exception(
                            "Could not link existing "
                            "UploadedFile %s to "
                            "attachment %s",
                            existing.pk,
                            getattr(
                                att,
                                "filename",
                                "?",
                            ),
                        )

            except Exception:
                logger.exception(
                    "Could not look up existing "
                    "UploadedFile for attachment %s",
                    getattr(
                        att,
                        "filename",
                        "?",
                    ),
                )

            needs_ingest.append(att)

        if not needs_ingest:
            return None, []

        try:
            batch = UploadService.create_batch(
                label=(
                    f"Chat upload "
                    f"({len(needs_ingest)} file(s))"
                ),
                user=user,
            )

        except Exception as exc:
            logger.exception(
                "Could not create chat upload batch: %s",
                exc,
            )

            raise

        newly_created_ids = []

        for att in needs_ingest:

            try:
                att.file.open("rb")

                record = UploadService.receive_file(
                    batch=batch,
                    uploaded=att.file,
                )

                att.uploaded_file = record

                att.save(
                    update_fields=[
                        "uploaded_file"
                    ]
                )

                newly_created_ids.append(
                    record.pk
                )

            except Exception:
                logger.exception(
                    "Could not persist chat attachment %s",
                    getattr(
                        att,
                        "filename",
                        "?",
                    ),
                )

                raise

            finally:

                try:
                    att.file.close()
                except Exception:
                    pass

        return (
            batch,
            newly_created_ids,
        )

    @staticmethod
    def get_response(
        message: str,
        user,
        *,
        file_attachments=None,
        workflow_id: int | None = None,
        workflow_option: str | None = None,
        conversation_history: list[dict] | None = None,
        conversation=None,
        on_status_update=None,
    ) -> tuple[str, list]:
        """
        Execute the AI stage of one chat turn.

        FILE READINESS
        --------------

        The caller must have already persisted UploadedFile records and
        queued/waited for extraction.

        This method intentionally does NOT call receive_file(),
        pipeline_ingest_task or extract_file_text_task.

        If an attachment is still processing, the AI agent is not started.
        """

        if not getattr(
            settings,
            "XAI_API_KEY",
            "",
        ):
            return (
                ChatService._offline_fallback(
                    message
                ),
                [],
            )

        attachments = list(
            file_attachments or []
        )

        # ---------------------------------------------------------------
        # Final safety barrier.
        #
        # Even though chat/tasks.py waits for files, do not allow another
        # caller to accidentally send an unparsed file into the agent.
        # ---------------------------------------------------------------

        not_ready = []

        for att in attachments:

            uploaded_file = getattr(
                att,
                "uploaded_file",
                None,
            )

            if not uploaded_file:
                not_ready.append(
                    getattr(
                        att,
                        "filename",
                        "uploaded file",
                    )
                )
                continue

            if getattr(
                uploaded_file,
                "parse_status",
                "",
            ) != "parsed":

                not_ready.append(
                    getattr(
                        uploaded_file,
                        "original_filename",
                        getattr(
                            att,
                            "filename",
                            "uploaded file",
                        ),
                    )
                )

        if not_ready:

            names = ", ".join(
                not_ready
            )

            logger.warning(
                "ChatService.get_response called "
                "before file extraction completed: %s",
                names,
            )

            return (
                "Your file(s) are still being processed. "
                "Please wait for extraction to finish "
                "before asking me to analyse them.\n\n"
                f"Files: {names}",
                [],
            )

        workflow = (
            ChatService
            ._resolve_workflow_for_message(
                message,
                attachments,
                workflow_id,
            )
        )

        batch = None

        try:
            response_text, job_id = (
                ChatService._run_agent(
                    message=message,
                    user=user,
                    batch=batch,
                    workflow=workflow,
                    conversation_history=(
                        conversation_history
                        or []
                    ),
                    conversation=conversation,
                    on_status_update=(
                        on_status_update
                    ),
                )
            )

        except Exception as exc:

            logger.exception(
                "ChatService agent run failed "
                "for user %s: %s",
                getattr(
                    user,
                    "username",
                    "?",
                ),
                exc,
            )

            return (
                f"Sorry, something went wrong: {exc}",
                [],
            )

        output_files = (
            ChatService
            ._collect_output_files(
                job_id
            )
        )

        if (
            output_files
            and "attach"
            not in response_text.lower()
        ):

            names = ", ".join(
                item["filename"]
                for item in output_files
            )

            response_text += (
                "\n\nGenerated file(s) attached: "
                f"{names}"
            )

        return (
            response_text,
            output_files,
        )

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

        from ai_engine.services import (
            AIEngineService
        )

        response_text, job_id = (
            AIEngineService.handle_chat_message(
                user=user,
                message=message,
                batch=batch,
                workflow=workflow,
                conversation_history=(
                    conversation_history or []
                ),
                conversation=conversation,
                on_status_update=(
                    on_status_update
                ),
            )
        )

        return (
            response_text,
            job_id,
        )

    @staticmethod
    def _collect_output_files(
        job_id,
    ) -> list:
        """
        Collect files written by tool handlers.
        """

        if not job_id:
            return []

        try:

            from tools.services import ToolService

            return (
                ToolService
                .collect_output_files_for_job(
                    job_id
                )
            )

        except Exception as exc:

            logger.warning(
                "Could not collect output files "
                "for job %s: %s",
                job_id,
                exc,
            )

            return []

    @staticmethod
    def _offline_fallback(
        message: str,
    ) -> str:

        return (
            _OFFLINE_RESPONSES.get(
                "simulation"
            )
            or
            _OFFLINE_RESPONSES.get(
                "general"
            )
        )