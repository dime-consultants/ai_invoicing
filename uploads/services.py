"""
UploadService — handles file ingestion into an UploadBatch.

Design for large files (chat-app friendly)
-----------------------------------------
1. Always save the file to media storage first (fast path).
2. For PDFs: cheaply count pages with pdfplumber (no full extract).
3. If page_count > ASYNC_PAGE_THRESHOLD (default 20):
     - set parse_status = "pending", extraction_deferred = True
     - queue extract_file_text_task (Celery) — returns immediately
4. Otherwise: extract text inline (small files / non-PDFs).
5. The agent never waits on large PDFs. It calls read_file(file_id,
   page_from, page_to) which extracts on-demand ranges from the raw PDF
   even while the background job is still running (or after it finishes).

This keeps HTTP / WebSocket chat turns responsive and lets Grok process
hundreds of pages across multiple tool calls.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import UploadBatch, UploadedFile

logger = logging.getLogger(__name__)

# Max characters stored in extracted_text before prompt-level truncation.
# This should be generous enough to keep the full document usable for AI
# processing; later tool calls still cap content per prompt for token budgets.
MAX_CHARS = getattr(settings, "UNSTRUCTURED_MAX_CHARS", 2_000_000)

# PDFs with more pages than this are extracted in a Celery worker
ASYNC_PAGE_THRESHOLD = getattr(settings, "ASYNC_PAGE_THRESHOLD", 20)
PDF_EXTRACTION_CHUNK_SIZE = getattr(settings, "PDF_EXTRACTION_CHUNK_SIZE", 50)

# Any file (regardless of extension) bigger than this is also deferred to a
# Celery worker rather than extracted inline. The PDF page-count check above
# only ever caught PDFs; a huge .xlsx/.csv/unstructured-parsed file had no
# size guard at all and always extracted inline, however large, blocking
# whichever request or worker triggered ingestion.
ASYNC_SIZE_THRESHOLD_BYTES = getattr(settings, "ASYNC_SIZE_THRESHOLD_BYTES", 5 * 1024 * 1024)  # 5MB
SPREADSHEET_CHECKPOINT_ROWS = getattr(settings, "SPREADSHEET_CHECKPOINT_ROWS", 5000)


# ─────────────────────────────────────────────────────────────────────────────
# Page counting (PDF only, cheap)
# ─────────────────────────────────────────────────────────────────────────────

def _count_pdf_pages(file_path: str) -> int | None:
    """
    Return the page count of a PDF without extracting any text.
    Used to decide whether to route to the async Celery pipeline.
    """
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return len(pdf.pages)
    except Exception as exc:
        logger.warning("Could not count pages for %s: %s", file_path, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text(
    file_path: str,
    extension: str,
    *,
    max_chars: int | None = None,
    on_chunk=None,
) -> str:
    """
    Extract plain text from a file for AI context / type detection.

    By default we keep the full extracted text in the database so the model can
    inspect the whole document; prompt-level tool calls still cap how much is
    injected into a single request.

    on_chunk(text_so_far), if given, is invoked periodically for the PDF and
    spreadsheet branches (every SPREADSHEET_CHECKPOINT_ROWS rows for xlsx/xls)
    so a caller running under a Celery soft time limit can checkpoint partial
    progress — see _extract_pdf_pages for the PDF version of the same
    contract. txt/csv/unstructured reads are single-shot and don't chunk.
    """
    limit = max_chars if max_chars is not None else None

    if extension in ("txt", "csv"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
                return text if limit is None else text[:limit]
        except Exception as exc:
            logger.warning("Plain text read failed for %s: %s", file_path, exc)
            return ""

    if extension == "pdf":
        return _extract_pdf_pages(file_path, page_from=1, page_to=None, max_chars=limit, on_chunk=on_chunk)

    if extension in ("xlsx", "xls"):
        checkpoint_every = SPREADSHEET_CHECKPOINT_ROWS
        try:
            parts, total = [], 0
            row_num = 0

            def _maybe_checkpoint():
                if on_chunk is not None and row_num % checkpoint_every == 0:
                    try:
                        on_chunk("\n".join(parts))
                    except Exception:
                        logger.warning("_extract_text: on_chunk checkpoint failed", exc_info=True)

            if extension == "xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        row_num += 1
                        line = "\t".join("" if c is None else str(c) for c in row)
                        if line.strip():
                            parts.append(line)
                            total += len(line)
                        _maybe_checkpoint()
                        if limit is not None and total >= limit:
                            break
                    if limit is not None and total >= limit:
                        break
                wb.close()
            else:  # xls
                import xlrd
                book = xlrd.open_workbook(file_path)
                for sheet in book.sheets():
                    for r in range(sheet.nrows):
                        row_num += 1
                        line = "\t".join(str(c) for c in sheet.row_values(r))
                        if line.strip():
                            parts.append(line)
                            total += len(line)
                        _maybe_checkpoint()
                        if limit is not None and total >= limit:
                            break
                    if limit is not None and total >= limit:
                        break
            return ("\n".join(parts) if limit is None else "\n".join(parts)[:limit])
        except Exception as exc:
            logger.warning("spreadsheet extraction failed for %s: %s", file_path, exc)

    try:
        from unstructured.partition.auto import partition
        elements = partition(filename=file_path)
        text = "\n\n".join(str(el) for el in elements if str(el).strip())
        return text if limit is None else text[:limit]
    except ImportError:
        logger.warning(
            "unstructured is not installed; '%s' files have no extracted_text. "
            "Install with: pip install 'unstructured[all-docs]'", extension
        )
        return ""
    except Exception as exc:
        logger.warning("unstructured extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_pdf_pages(
    file_path: str,
    *,
    page_from: int = 1,
    page_to: int | None = None,
    max_chars: int | None = None,
    on_chunk=None,
) -> str:
    """
    Extract text from a PDF page range (1-indexed, inclusive).

    Large PDFs are processed in page chunks and merged back together so the
    background worker never tries to hold the entire document in one giant
    in-memory buffer. This keeps the worker stable even for very large files.

    on_chunk(text_so_far), if given, is called after every
    PDF_EXTRACTION_CHUNK_SIZE page group finishes, with the text merged so
    far. Callers running under a Celery soft time limit (extract_file_text_task)
    use this to checkpoint partial progress, so a mid-extraction timeout on a
    1000-page document still has real, non-empty text to save instead of
    losing everything extracted before the deadline. A checkpoint failure is
    logged and swallowed — it must never abort the extraction itself.
    """
    limit = max_chars if max_chars is not None else MAX_CHARS
    try:
        import pdfplumber
        parts: list[str] = []
        total = 0
        with pdfplumber.open(file_path) as pdf:
            n = len(pdf.pages)
            start = max(1, page_from) - 1
            end = n if page_to is None else min(page_to, n)
            chunk_size = max(1, PDF_EXTRACTION_CHUNK_SIZE)

            for chunk_start in range(start, end, chunk_size):
                chunk_end = min(chunk_start + chunk_size, end)
                for idx in range(chunk_start, chunk_end):
                    page = pdf.pages[idx]
                    t = page.extract_text() or ""
                    if t:
                        header = f"--- Page {idx + 1} ---\n"
                        parts.append(header + t)
                        total += len(header) + len(t)
                    page.flush_cache()
                    if limit and total >= limit:
                        parts.append(f"\n[… truncated at {limit} characters …]")
                        break
                if on_chunk is not None:
                    try:
                        on_chunk("\n\n".join(parts))
                    except Exception:
                        logger.warning("_extract_pdf_pages: on_chunk checkpoint failed", exc_info=True)
                if limit and total >= limit:
                    break
        return "\n\n".join(parts)
    except Exception as exc:
        logger.warning("pdfplumber range extract failed for %s: %s", file_path, exc)
        return ""


def extract_pdf_page_range(
    file_path: str,
    page_from: int = 1,
    page_to: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """
    Public helper used by the read_file tool and Celery task.

    Returns:
        {
          "ok": True,
          "text": str,
          "page_from": int,
          "page_to": int,
          "page_count": int,
          "chars": int,
        }
    """
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)
    except Exception as exc:
        return {"ok": False, "error": f"Cannot open PDF: {exc}"}

    start = max(1, page_from)
    end   = page_count if page_to is None else min(max(page_to, start), page_count)

    text = _extract_pdf_pages(
        file_path,
        page_from=start,
        page_to=end,
        max_chars=max_chars,
    )
    return {
        "ok": True,
        "text": text,
        "page_from": start,
        "page_to": end,
        "page_count": page_count,
        "chars": len(text),
    }


# ─────────────────────────────────────────────────────────────────────────────
# UploadService
# ─────────────────────────────────────────────────────────────────────────────

class UploadService:

    @staticmethod
    @transaction.atomic
    def create_batch(*, label: str, description: str = "", user) -> UploadBatch:
        """Create a new UploadBatch owned by `user`."""
        return UploadBatch.objects.create(
            label=label,
            description=description,
            uploaded_by=user,
            status="pending",
        )

    @staticmethod
    @transaction.atomic
    def ingest_file(batch: UploadBatch, uploaded_file) -> UploadedFile:
        """
        Save one uploaded file into `batch`.

        Large files — either a PDF with page_count > ASYNC_PAGE_THRESHOLD, or
        ANY file (xlsx/xls/csv/unstructured-parsed) over ASYNC_SIZE_THRESHOLD_BYTES:
          - page_count (PDFs) is recorded
          - parse_status stays "pending", extraction_deferred=True
          - extract_file_text_task is queued and the call returns immediately

        Small files:
          - text is extracted inline and parse_status becomes "parsed"

        Returns the UploadedFile record (may still be pending for large files).
        """
        original_name = Path(uploaded_file.name).name
        extension     = Path(original_name).suffix.lstrip(".").lower()
        mime_type, _  = mimetypes.guess_type(original_name)
        file_size     = (
            uploaded_file.size
            if hasattr(uploaded_file, "size")
            else uploaded_file.seek(0, 2) or uploaded_file.tell()
        )

        # 1. Persist file to storage
        record = UploadedFile.objects.create(
            batch             = batch,
            file              = uploaded_file,
            original_filename = original_name,
            file_size_bytes   = file_size or 0,
            mime_type         = mime_type or "",
            extension         = extension,
            parse_status      = "pending",
        )

        # 2. Cheap page count for PDFs
        page_count = None
        if extension == "pdf":
            try:
                saved_path = record.file.path
                page_count = _count_pdf_pages(saved_path)
                if page_count is not None:
                    record.page_count = page_count
                    record.save(update_fields=["page_count"])
            except Exception as exc:
                logger.warning("Page count failed for %s: %s", original_name, exc)

        # 3. Decide sync vs async extraction
        is_large_pdf = (
            extension == "pdf"
            and page_count is not None
            and page_count > ASYNC_PAGE_THRESHOLD
        )
        is_large_by_size = bool(file_size) and file_size > ASYNC_SIZE_THRESHOLD_BYTES
        is_large = is_large_pdf or is_large_by_size

        if is_large:
            record.extraction_deferred = True
            record.parse_status = "pending"
            record.save(update_fields=["extraction_deferred", "parse_status"])
            UploadService._refresh_batch_counters(batch)

            # Queue background extraction (do not block the request).
            #
            # This MUST run on_commit: ingest_file is atomic (and ingest_many wraps
            # a whole batch in one transaction), so dispatching inline races the
            # commit — Redis delivers in milliseconds and the worker's
            # UploadedFile.objects.get(pk=...) raises DoesNotExist, which the task
            # reports as "file_not_found" without retrying. The file would then sit
            # at pending/extraction_deferred forever.
            file_pk = record.pk

            def _queue_extraction():
                from uploads.tasks import extract_file_text_task
                try:
                    extract_file_text_task.delay(file_pk)
                    logger.info(
                        "Queued async extraction for large %s file %s (%s, file_id=%s)",
                        extension,
                        original_name,
                        f"{page_count} pages" if is_large_pdf else f"{file_size} bytes",
                        file_pk,
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed to queue extract_file_text_task for file %s: %s",
                        file_pk, exc,
                    )
                    # Broker unreachable — fall back to inline so the file is still
                    # usable. Re-read the row: we are past commit and outside the
                    # original transaction.
                    try:
                        rec = UploadedFile.objects.select_related("batch").get(pk=file_pk)
                    except UploadedFile.DoesNotExist:
                        return
                    rec.extraction_deferred = False
                    rec.save(update_fields=["extraction_deferred"])
                    UploadService._extract_and_save(rec)
                    UploadService._refresh_batch_counters(rec.batch)

            transaction.on_commit(_queue_extraction)
            return record

        # 4. Small file / non-PDF — extract inline
        UploadService._extract_and_save(record)
        UploadService._refresh_batch_counters(batch)
        return record

    @staticmethod
    def _extract_and_save(record: UploadedFile) -> None:
        """Run text extraction and update the UploadedFile row."""
        try:
            saved_path = record.file.path
            text = _extract_text(saved_path, record.extension)
            if not text:
                # Extraction ran to completion and legitimately found nothing
                # (scanned/image-only PDF, or a missing parser backend). That is
                # a terminal outcome, not "still queued" — leaving it "pending"
                # pins the batch at "processing" forever with nothing to re-run it.
                logger.warning(
                    "Extraction produced no text for %s (file_id=%s); marking parsed with empty text.",
                    record.original_filename, record.pk,
                )
            record.extracted_text = text or ""
            record.parse_status   = "parsed"
            record.parsed_at      = timezone.now()
            record.parse_error    = ""
            record.save(update_fields=[
                "extracted_text", "parse_status", "parsed_at", "parse_error",
            ])
        except Exception as exc:
            logger.exception("Text extraction failed for %s: %s", record.original_filename, exc)
            record.parse_status = "parse_error"
            record.parse_error  = str(exc)
            record.save(update_fields=["parse_status", "parse_error"])

    @staticmethod
    @transaction.atomic
    def ingest_many(batch: UploadBatch, uploaded_files: list, *, user=None) -> list[UploadedFile]:
        """Ingest a list of uploaded files into a batch. Returns saved records."""
        records = []
        for uf in uploaded_files:
            try:
                record = UploadService.ingest_file(batch, uf)
                records.append(record)
            except Exception as exc:
                logger.exception(
                    "Failed to ingest %s into batch %s: %s",
                    getattr(uf, "name", "?"), batch.pk, exc,
                )
        return records

    @staticmethod
    def get_batch_files(batch: UploadBatch) -> list[UploadedFile]:
        """Return all UploadedFile records for a batch, ordered by upload time."""
        return list(batch.files.order_by("uploaded_at"))

    @staticmethod
    def _refresh_batch_counters(batch: UploadBatch) -> None:
        """
        Recount file_count, processed_count, error_count from the DB
        and update the batch status accordingly.
        """
        files  = batch.files.all()
        total  = files.count()
        parsed = files.filter(parse_status="parsed").count()
        errors = files.filter(parse_status="parse_error").count()
        # "skipped" is terminal too. Counting it toward `total` but toward none of
        # the outcome buckets meant a skipped file could never satisfy the
        # "everything finished" test, pinning the batch at "processing" forever.
        skipped = files.filter(parse_status="skipped").count()
        pending_or_parsing = files.filter(
            parse_status__in=("pending", "parsing")
        ).count()

        if total == 0:
            new_status = "pending"
        elif errors == total:
            new_status = "failed"
        elif pending_or_parsing > 0:
            new_status = "processing"
        elif parsed + errors + skipped == total:
            new_status = "completed" if errors == 0 else "partial"
        else:
            new_status = "processing"

        UploadBatch.objects.filter(pk=batch.pk).update(
            file_count      = total,
            processed_count = parsed,
            error_count     = errors,
            status          = new_status,
        )
        batch.refresh_from_db()

    # ── Helpers used by Celery task & tools ───────────────────────────────────

    @staticmethod
    def mark_parsing(file_id: int) -> UploadedFile | None:
        try:
            uf = UploadedFile.objects.select_related("batch").get(pk=file_id)
        except UploadedFile.DoesNotExist:
            return None
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])
        UploadService._refresh_batch_counters(uf.batch)
        return uf

    @staticmethod
    def complete_extraction(
        file_id: int,
        *,
        text: str,
        page_count: int | None = None,
        error: str | None = None,
    ) -> UploadedFile | None:
        """
        Called by the Celery task when extraction finishes (success or failure).
        """
        try:
            uf = UploadedFile.objects.select_related("batch").get(pk=file_id)
        except UploadedFile.DoesNotExist:
            return None

        if error:
            uf.parse_status = "parse_error"
            uf.parse_error  = error[:2000]
            uf.extracted_text = ""
        else:
            if not text:
                # See _extract_and_save: a completed extraction that yielded no
                # text is terminal. "pending" would strand the file and its batch.
                logger.warning(
                    "Background extraction produced no text for file_id=%s; "
                    "marking parsed with empty text.", file_id,
                )
            uf.extracted_text = text or ""
            uf.parse_status   = "parsed"
            uf.parse_error    = ""
            uf.parsed_at      = timezone.now()
            if page_count is not None:
                uf.page_count = page_count

        uf.save(update_fields=[
            "extracted_text", "parse_status", "parse_error",
            "parsed_at", "page_count",
        ])

        # Detection may have run while extraction was still deferred, i.e. against
        # empty text — that verdict is provisional and nothing else would ever
        # recompute it. Now that real text exists, redo it. Only when the current
        # verdict is absent or low-confidence, so a high-confidence or manually
        # corrected label is never clobbered.
        if not error and text and uf.detection_confidence in (None, "", "low"):
            try:
                from tools.handlers import detect_file_type
                detect_file_type(uf.pk)
                uf.refresh_from_db()
            except Exception as exc:
                logger.warning(
                    "Re-detection after extraction failed for file %s: %s", uf.pk, exc,
                )

        UploadService._refresh_batch_counters(uf.batch)
        return uf