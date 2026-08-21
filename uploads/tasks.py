# uploads/tasks.py

"""
Celery tasks for the uploads app.

Pipeline
--------

pipeline_ingest_task
    1. Load UploadedFile.
    2. Calculate checksum.
    3. Detect MIME type.
    4. Count PDF pages.
    5. Emit file.stored.
    6. Refresh batch counters.
    7. Queue extract_file_text_task.
    8. Emit file.queued.

extract_file_text_task
    1. Mark file as parsing.
    2. Emit file.extracting progress events.
    3. Extract text incrementally.
    4. Checkpoint partial text during extraction.
    5. Store the full extracted text.
    6. Mark extraction as complete or failed.
    7. Preserve partial extraction on soft timeout.

reextract_file_task
    Resets a file and dispatches a fresh extraction task.

IMPORTANT
---------
ChatService does not wait synchronously for these tasks.

The chat task persists UploadedFile records, queues this pipeline, and then
hands control to a non-blocking wait task. Only after parse_status becomes
"parsed" does the AI agent start.
"""

from __future__ import annotations

import logging
import mimetypes

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

from .events import emit
from .models import UploadedFile
from .services import (
    UploadService,
    _extract_pdf_pages,
    _extract_text,
)

logger = logging.getLogger(__name__)


# Large file extraction limits.
_SOFT_LIMIT = 60 * 15
_HARD_LIMIT = 60 * 20


@shared_task(
    bind=True,
    acks_late=True,
    max_retries=3,
)
def pipeline_ingest_task(self, file_id: int):
    """
    Perform the initial background ingestion work for a file.

    This task performs metadata processing only. Once complete it queues
    extract_file_text_task.

    The chat pipeline does NOT start the AI agent from here. ChatService's
    orchestration waits until extraction reports parse_status="parsed".
    """

    try:
        uf = (
            UploadedFile.objects
            .select_related("batch")
            .get(pk=file_id)
        )

        saved_path = uf.file.path

        # ---------------------------------------------------------------
        # Calculate checksum.
        # ---------------------------------------------------------------

        uf.checksum_sha256 = _file_checksum(saved_path)

        # ---------------------------------------------------------------
        # Detect MIME type.
        # ---------------------------------------------------------------

        mime, _ = mimetypes.guess_type(
            uf.original_filename
        )

        uf.mime_type = mime or ""

        # ---------------------------------------------------------------
        # Count PDF pages.
        # ---------------------------------------------------------------

        if uf.extension == "pdf":
            uf.page_count = _count_pdf_pages(
                saved_path
            )

        uf.save(
            update_fields=[
                "checksum_sha256",
                "mime_type",
                "page_count",
            ]
        )

        # ---------------------------------------------------------------
        # Notify clients that the file has been stored.
        # ---------------------------------------------------------------

        emit(
            "file.stored",
            batch=uf.batch,
            file=uf,
            payload={
                "checksum": uf.checksum_sha256,
                "pageCount": uf.page_count,
            },
        )

        # ---------------------------------------------------------------
        # Update batch counters.
        # ---------------------------------------------------------------

        UploadService._refresh_batch_counters(
            uf.batch
        )

        # ---------------------------------------------------------------
        # Queue extraction.
        # ---------------------------------------------------------------

        async_result = extract_file_text_task.delay(
            file_id
        )

        emit(
            "file.queued",
            batch=uf.batch,
            file=uf,
            payload={
                "taskId": async_result.id,
            },
        )

        logger.info(
            "pipeline_ingest_task complete: "
            "file_id=%s task_id=%s",
            file_id,
            async_result.id,
        )

        return {
            "ok": True,
            "file_id": file_id,
            "task_id": async_result.id,
        }

    except UploadedFile.DoesNotExist:
        logger.error(
            "pipeline_ingest_task: "
            "UploadedFile %s not found",
            file_id,
        )

        return {
            "ok": False,
            "error": "file_not_found",
        }

    except Exception as exc:
        logger.exception(
            "pipeline_ingest_task failed: "
            "file_id=%s error=%s",
            file_id,
            exc,
        )

        # ---------------------------------------------------------------
        # Retry transient ingestion failures.
        #
        # When retries are exhausted, mark the file as failed so that a
        # waiting chat task never waits forever.
        # ---------------------------------------------------------------

        if self.request.retries < self.max_retries:
            try:
                raise self.retry(
                    exc=exc,
                    countdown=10,
                )
            except MaxRetriesExceededError:
                pass

        safe_error = (
            f"Initial file ingestion failed: {str(exc)[:1800]}"
        )

        try:
            uf = (
                UploadedFile.objects
                .select_related("batch")
                .get(pk=file_id)
            )

            UploadService.complete_extraction(
                file_id,
                text="",
                error=safe_error,
            )

            emit(
                "file.parse_error",
                batch=uf.batch,
                file=uf,
                payload={
                    "error": safe_error,
                },
            )

        except Exception:
            logger.exception(
                "Could not mark failed ingestion for file_id=%s",
                file_id,
            )

        return {
            "ok": False,
            "file_id": file_id,
            "error": safe_error,
        }


@shared_task(
    bind=True,
    max_retries=2,
    soft_time_limit=_SOFT_LIMIT,
    time_limit=_HARD_LIMIT,
    acks_late=True,
)
def extract_file_text_task(self, file_id: int):
    """
    Extract text from an UploadedFile in the background.

    Supports:
    - Large PDFs
    - XLSX/XLS files
    - CSV files
    - Other supported extraction formats

    Extraction progress is emitted through PipelineEvent/WebSocket events.

    On successful completion UploadService.complete_extraction() must leave
    the UploadedFile in parse_status="parsed".

    On terminal failure it must leave the UploadedFile in a failure state
    such as parse_status="error".
    """

    try:
        uf = (
            UploadedFile.objects
            .select_related("batch")
            .get(pk=file_id)
        )

    except UploadedFile.DoesNotExist:
        logger.error(
            "extract_file_text_task: "
            "UploadedFile %s not found",
            file_id,
        )

        return {
            "ok": False,
            "error": "file_not_found",
        }

    # ---------------------------------------------------------------
    # Already ready — do not reprocess.
    # ---------------------------------------------------------------

    if (
        uf.parse_status == "parsed"
        and uf.extracted_text is not None
    ):
        logger.info(
            "extract_file_text_task: "
            "file %s already parsed — skipping",
            file_id,
        )

        return {
            "ok": True,
            "skipped": True,
            "file_id": file_id,
        }

    # ---------------------------------------------------------------
    # Mark extraction as active.
    # ---------------------------------------------------------------

    marked_file = UploadService.mark_parsing(
        file_id
    )

    if marked_file is not None:
        uf = marked_file

    partial_text = ""

    emit(
        "file.extracting",
        batch=uf.batch,
        file=uf,
        payload={
            "percent": 0,
        },
    )

    def _checkpoint(text_so_far: str) -> None:
        """
        Save progress in memory and emit a WebSocket progress event.
        """

        nonlocal partial_text

        partial_text = text_so_far or ""

        percent = min(
            99,
            int(
                len(partial_text)
                / max(
                    uf.file_size_bytes or 1,
                    1,
                )
                * 100
            ),
        )

        emit(
            "file.extracting",
            batch=uf.batch,
            file=uf,
            payload={
                "percent": percent,
                "chars": len(partial_text),
            },
        )

    try:
        path = uf.file.path
        extension = (
            uf.extension or ""
        ).lower()

        # -----------------------------------------------------------
        # PDF extraction.
        # -----------------------------------------------------------

        if extension == "pdf":

            text = _extract_pdf_pages(
                path,
                page_from=1,
                page_to=None,
                max_chars=None,
                on_chunk=_checkpoint,
            )

            page_count = uf.page_count

            if page_count is None:
                try:
                    import pdfplumber

                    with pdfplumber.open(path) as pdf:
                        page_count = len(pdf.pages)

                except Exception:
                    logger.exception(
                        "Could not determine PDF page count "
                        "for file_id=%s",
                        file_id,
                    )

                    page_count = None

        # -----------------------------------------------------------
        # Generic extraction.
        # -----------------------------------------------------------

        else:

            text = _extract_text(
                path,
                extension,
                on_chunk=_checkpoint,
            )

            page_count = uf.page_count

        # -----------------------------------------------------------
        # Final extraction result.
        # -----------------------------------------------------------

        partial_text = text or ""

        UploadService.complete_extraction(
            file_id,
            text=partial_text,
            page_count=page_count,
        )

        emit(
            "file.parsed",
            batch=uf.batch,
            file=uf,
            payload={
                "percent": 100,
                "pageCount": page_count,
                "chars": len(partial_text),
            },
        )

        logger.info(
            "extract_file_text_task complete: "
            "file_id=%s pages=%s chars=%s",
            file_id,
            page_count,
            len(partial_text),
        )

        return {
            "ok": True,
            "file_id": file_id,
            "page_count": page_count,
            "chars": len(partial_text),
        }

    except SoftTimeLimitExceeded:

        logger.error(
            "extract_file_text_task soft time limit: "
            "file_id=%s",
            file_id,
        )

        # -----------------------------------------------------------
        # Preserve partial extraction.
        # -----------------------------------------------------------

        if partial_text:

            UploadService.complete_extraction(
                file_id,
                text=partial_text,
            )

            emit(
                "file.parsed",
                batch=uf.batch,
                file=uf,
                payload={
                    "partial": True,
                    "chars": len(partial_text),
                },
            )

            return {
                "ok": True,
                "file_id": file_id,
                "partial": True,
                "chars": len(partial_text),
            }

        error = (
            "Extraction timed out. "
            "Read_file page ranges are available as a fallback."
        )

        UploadService.complete_extraction(
            file_id,
            text="",
            error=error,
        )

        emit(
            "file.parse_error",
            batch=uf.batch,
            file=uf,
            payload={
                "error": error,
            },
        )

        return {
            "ok": False,
            "file_id": file_id,
            "error": error,
        }

    except Exception as exc:

        logger.exception(
            "extract_file_text_task failed: "
            "file_id=%s error=%s",
            file_id,
            exc,
        )

        safe_error = str(exc)[:2000]

        # -----------------------------------------------------------
        # Preserve partial extraction.
        # -----------------------------------------------------------

        if partial_text:

            UploadService.complete_extraction(
                file_id,
                text=partial_text,
            )

            emit(
                "file.parsed",
                batch=uf.batch,
                file=uf,
                payload={
                    "partial": True,
                    "chars": len(partial_text),
                },
            )

            return {
                "ok": True,
                "file_id": file_id,
                "partial": True,
                "chars": len(partial_text),
            }

        # -----------------------------------------------------------
        # Retry transient extraction failures.
        # -----------------------------------------------------------

        if self.request.retries < self.max_retries:

            try:
                raise self.retry(
                    exc=exc,
                    countdown=30,
                )
            except MaxRetriesExceededError:
                logger.error(
                    "extract_file_text_task: "
                    "retries exhausted for file_id=%s",
                    file_id,
                )

        # -----------------------------------------------------------
        # Terminal failure.
        #
        # IMPORTANT:
        # The waiting chat task relies on this terminal state.
        # -----------------------------------------------------------

        UploadService.complete_extraction(
            file_id,
            text="",
            error=safe_error,
        )

        emit(
            "file.parse_error",
            batch=uf.batch,
            file=uf,
            payload={
                "error": safe_error,
            },
        )

        return {
            "ok": False,
            "file_id": file_id,
            "partial": False,
            "error": safe_error,
        }


@shared_task(
    bind=True,
    max_retries=1,
)
def reextract_file_task(self, file_id: int):
    """
    Force a fresh extraction.

    Intended for:
    - Admin actions
    - parse_error recovery
    - Manual reprocessing
    """

    try:

        uf = (
            UploadedFile.objects
            .select_related("batch")
            .get(pk=file_id)
        )

    except UploadedFile.DoesNotExist:

        return {
            "ok": False,
            "error": "file_not_found",
        }

    # Reset extraction state.
    uf.parse_status = "pending"
    uf.parse_error = ""
    uf.extracted_text = ""

    uf.save(
        update_fields=[
            "parse_status",
            "parse_error",
            "extracted_text",
        ]
    )

    UploadService._refresh_batch_counters(
        uf.batch
    )

    async_result = extract_file_text_task.delay(
        file_id
    )

    return {
        "ok": True,
        "file_id": file_id,
        "task_id": async_result.id,
    }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _file_checksum(path: str) -> str:
    """
    Calculate a SHA-256 checksum without loading the entire file
    into memory.
    """

    import hashlib

    sha256 = hashlib.sha256()

    with open(path, "rb") as file:

        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            sha256.update(chunk)

    return sha256.hexdigest()


def _count_pdf_pages(path: str) -> int | None:
    """
    Return the number of pages in a PDF.
    """

    try:

        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)

    except Exception:

        logger.exception(
            "Failed to count PDF pages: %s",
            path,
        )

        return None