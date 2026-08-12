"""
Celery tasks for the uploads app.

extract_file_text_task
----------------------
Background extraction for any file whose extraction was deferred by
UploadService.ingest_file — large PDFs (page_count > ASYNC_PAGE_THRESHOLD)
and any file over ASYNC_SIZE_THRESHOLD_BYTES regardless of extension
(xlsx/xls/csv/unstructured-parsed).

Flow
----
1. Load UploadedFile, mark parse_status="parsing".
2. Extract page-by-page (PDF) or row-by-row (spreadsheet), checkpointing
   partial progress via on_chunk so a soft-time-limit timeout still saves
   real text instead of nothing (memory-safe flush_cache for PDFs).
3. Store a truncated preview in extracted_text (MAX_CHARS).
4. Mark parse_status="parsed" (or "parse_error") and refresh batch counters.
5. Signals (uploads/signals.py) push WS notifications so the chat UI can
   re-enable "file ready" state and the agent can proceed.

The agent does NOT need to wait for this task. It can call
read_file(file_id, page_from, page_to) at any time — that path reads the
raw PDF directly and never depends on extracted_text being complete.
"""

from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

logger = logging.getLogger(__name__)

# Large PDFs can take a while; allow generous limits.
_SOFT_LIMIT = 60 * 15   # 15 min soft
_HARD_LIMIT = 60 * 20   # 20 min hard


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

    Primarily used for large PDFs that exceed ASYNC_PAGE_THRESHOLD.
    Safe to call multiple times (idempotent with respect to final status).
    """
    from django.conf import settings
    from uploads.models import UploadedFile
    from uploads.services import (
        UploadService,
        _extract_pdf_pages,
        _extract_text,
        MAX_CHARS,
    )

    try:
        uf = UploadedFile.objects.select_related("batch").get(pk=file_id)
    except UploadedFile.DoesNotExist:
        logger.error("extract_file_text_task: UploadedFile %s not found", file_id)
        return {"ok": False, "error": "file_not_found"}

    if uf.parse_status == "parsed" and uf.extracted_text:
        logger.info("extract_file_text_task: file %s already parsed — skip", file_id)
        return {"ok": True, "skipped": True, "file_id": file_id}

    UploadService.mark_parsing(file_id)
    partial_text = ""

    def _checkpoint(text_so_far: str) -> None:
        # Called by _extract_pdf_pages / _extract_text after each chunk
        # (page group / row batch) so a SoftTimeLimitExceeded below always
        # has real partial text to save instead of the empty string the
        # all-or-nothing extraction used to leave behind.
        nonlocal partial_text
        partial_text = text_so_far

    try:
        path = uf.file.path
        extension = uf.extension or ""

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
                    page_count = None
        else:
            text = _extract_text(path, extension, on_chunk=_checkpoint)
            page_count = uf.page_count

        partial_text = text or ""
        UploadService.complete_extraction(
            file_id,
            text=partial_text,
            page_count=page_count,
        )
        logger.info(
            "extract_file_text_task complete: file_id=%s pages=%s chars=%s",
            file_id, page_count, len(partial_text),
        )
        return {
            "ok": True,
            "file_id": file_id,
            "page_count": page_count,
            "chars": len(partial_text),
        }

    except SoftTimeLimitExceeded:
        logger.error("extract_file_text_task soft time limit: file_id=%s", file_id)
        UploadService.complete_extraction(
            file_id,
            text=partial_text or "",
            error=None if partial_text else "Extraction timed out. Read_file page ranges are available as a fallback.",
        )
        return {"ok": True, "file_id": file_id, "partial": True, "chars": len(partial_text or "")}

    except Exception as exc:
        logger.exception("extract_file_text_task failed: file_id=%s error=%s", file_id, exc)
        safe_error = str(exc)[:2000]
        if partial_text:
            UploadService.complete_extraction(file_id, text=partial_text, error=None)
            return {"ok": True, "file_id": file_id, "partial": True, "chars": len(partial_text)}

        if self.request.retries < self.max_retries:
            try:
                # self.retry() already re-queued the next attempt (via its own
                # apply_async) before raising Retry — Retry itself only signals
                # "stop running now" to Celery's task-execution wrapper, which
                # lives outside this function and must see it to record the
                # retry state. Catching it here would swallow that signal
                # without undoing the re-queue, so it must propagate untouched.
                raise self.retry(exc=exc, countdown=30)
            except MaxRetriesExceededError:
                # self.retry() itself decided no attempts remain (it raises
                # this instead of Retry in that case) — fall through to the
                # honest terminal-failure path below.
                logger.error("extract_file_text_task: retries exhausted for file_id=%s", file_id)

        UploadService.complete_extraction(file_id, text="", error=safe_error)
        return {"ok": False, "file_id": file_id, "partial": False, "error": safe_error}


@shared_task(bind=True, max_retries=1)
def reextract_file_task(self, file_id: int):
    """
    Force re-extraction (e.g. after a manual admin action or parse_error recovery).
    Resets status to pending then runs the same pipeline.
    """
    from uploads.models import UploadedFile
    from uploads.services import UploadService

    try:
        uf = UploadedFile.objects.get(pk=file_id)
    except UploadedFile.DoesNotExist:
        return {"ok": False, "error": "file_not_found"}

    uf.parse_status = "pending"
    uf.parse_error  = ""
    uf.extracted_text = ""
    uf.save(update_fields=["parse_status", "parse_error", "extracted_text"])
    UploadService._refresh_batch_counters(uf.batch)

    # Dispatch, don't call. A direct call runs in this worker with
    # request.called_directly=True, which makes self.retry() re-raise instead of
    # retrying (so max_retries=2 silently never applies) and bypasses that task's
    # own soft_time_limit/time_limit — the limits that exist precisely because
    # this is the long-running path.
    async_result = extract_file_text_task.delay(file_id)
    return {"ok": True, "file_id": file_id, "task_id": async_result.id}