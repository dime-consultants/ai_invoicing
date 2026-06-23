# uploads/services.py
"""
UploadService — handles file ingestion into an UploadBatch.

Responsibilities
----------------
1.  Save the uploaded file to media storage (UploadedFile record).
2.  Extract plain text from the file (and, for PDFs, record page_count
    so callers like chat/services.py can decide whether a file is large
    enough to route to the async Celery pipeline instead of processing
    inline in an HTTP request).
3.  Store the extracted text in UploadedFile.extracted_text so the
    ai_engine can inject it into the LLM context window without re-reading
    the raw bytes on every request.
4.  Update batch counters (file_count, processed_count, error_count).

`unstructured` supports: txt, pdf, docx, xlsx, pptx, html, images, eml, and more.
No API key is required for the local package.
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

# Max characters stored in extracted_text (keeps DB rows reasonable)
MAX_CHARS = getattr(settings, "UNSTRUCTURED_MAX_CHARS", 50_000)


# ─────────────────────────────────────────────────────────────────────────────
# Page counting (PDF only, for now)
# ─────────────────────────────────────────────────────────────────────────────

def _count_pdf_pages(file_path: str) -> int | None:
    """
    Return the page count of a PDF without extracting any text — used to
    decide, cheaply and up front, whether a file is large enough to route
    to the async Celery pipeline (see chat/services.py:ASYNC_PAGE_THRESHOLD)
    rather than processing inline in an HTTP request thread.
    """
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            return len(pdf.pages)
    except Exception as exc:
        logger.warning("Could not count pages for %s: %s", file_path, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text(file_path: str, extension: str) -> str:
    """
    Extract plain text from a file for AI context / type detection.

    Uses lightweight, already-installed libraries per format:
      - txt/csv  : direct utf-8 read
      - pdf      : pdfplumber (page-by-page, stops once MAX_CHARS reached)
      - xlsx/xls : openpyxl (xlsx) / xlrd (xls), cells flattened to text
    Falls back to `unstructured` for anything else (docx, html, …) if it is
    installed, otherwise returns "" so ingestion still succeeds.

    NOTE: this still caps at MAX_CHARS for the *stored* extracted_text
    column — it is a preview/search-context field, not the full document.
    Tool handlers that need the complete document (e.g.
    tools/handlers.py:extract_safaricom_bill) re-open and re-read the raw
    file directly rather than relying on this truncated column.
    """
    if extension in ("txt", "csv"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[:MAX_CHARS]
        except Exception as exc:
            logger.warning("Plain text read failed for %s: %s", file_path, exc)
            return ""

    if extension == "pdf":
        try:
            import pdfplumber
            parts, total = [], 0
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    if t:
                        parts.append(t)
                        total += len(t)
                    # Release this page's cached objects (chars, lines,
                    # rects, etc.) immediately — without this, memory grows
                    # roughly linearly with page count across a large PDF.
                    page.flush_cache()
                    if total >= MAX_CHARS:
                        break
            return "\n\n".join(parts)[:MAX_CHARS]
        except Exception as exc:
            logger.warning("pdfplumber extraction failed for %s: %s", file_path, exc)
            # fall through to unstructured

    if extension in ("xlsx", "xls"):
        try:
            parts, total = [], 0
            if extension == "xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        line = "\t".join("" if c is None else str(c) for c in row)
                        if line.strip():
                            parts.append(line)
                            total += len(line)
                        if total >= MAX_CHARS:
                            break
                    if total >= MAX_CHARS:
                        break
                wb.close()
            else:  # xls
                import xlrd
                book = xlrd.open_workbook(file_path)
                for sheet in book.sheets():
                    for r in range(sheet.nrows):
                        line = "\t".join(str(c) for c in sheet.row_values(r))
                        if line.strip():
                            parts.append(line)
                            total += len(line)
                        if total >= MAX_CHARS:
                            break
                    if total >= MAX_CHARS:
                        break
            return "\n".join(parts)[:MAX_CHARS]
        except Exception as exc:
            logger.warning("spreadsheet extraction failed for %s: %s", file_path, exc)
            # fall through to unstructured

    try:
        from unstructured.partition.auto import partition

        elements = partition(filename=file_path)
        text = "\n\n".join(str(el) for el in elements if str(el).strip())
        return text[:MAX_CHARS]

    except ImportError:
        logger.warning(
            "unstructured is not installed; '%s' files have no extracted_text. "
            "Install with: pip install 'unstructured[all-docs]'", extension
        )
        return ""

    except Exception as exc:
        logger.warning("unstructured extraction failed for %s: %s", file_path, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# UploadService
# ─────────────────────────────────────────────────────────────────────────────

class UploadService:

    @staticmethod
    @transaction.atomic
    def create_batch(*, label: str, description: str = "", user) -> UploadBatch:
        """
        Create a new UploadBatch owned by `user`.
        """
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
        Save one uploaded file into `batch`, extract its text, and (for
        PDFs) record its page_count so callers can decide whether to route
        processing through the async Celery pipeline.

        Parameters
        ----------
        batch         UploadBatch to attach the file to.
        uploaded_file Django InMemoryUploadedFile / TemporaryUploadedFile
                      (from request.FILES) or any file-like with a .name attribute.

        Returns
        -------
        UploadedFile  — the saved record with extracted_text (and, for
                        PDFs, page_count) populated.
        """
        original_name = Path(uploaded_file.name).name
        extension     = Path(original_name).suffix.lstrip(".").lower()
        mime_type, _  = mimetypes.guess_type(original_name)
        file_size     = (
            uploaded_file.size
            if hasattr(uploaded_file, "size")
            else uploaded_file.seek(0, 2) or uploaded_file.tell()
        )

        # 1. Create the DB record — Django saves the file to MEDIA_ROOT
        record = UploadedFile.objects.create(
            batch             = batch,
            file              = uploaded_file,
            original_filename = original_name,
            file_size_bytes   = file_size or 0,
            mime_type         = mime_type or "",
            extension         = extension,
            parse_status      = "pending",
        )

        # 2. Page count (PDF only) — cheap, separate pass, used purely for
        #    the async-routing decision in chat/services.py.
        if extension == "pdf":
            try:
                saved_path = record.file.path
                page_count = _count_pdf_pages(saved_path)
                if page_count is not None and hasattr(record, "page_count"):
                    record.page_count = page_count
                    record.save(update_fields=["page_count"])
            except Exception as exc:
                logger.warning("Page count failed for %s: %s", original_name, exc)

        # 3. Extract text from the saved file (read from disk, not from memory)
        try:
            saved_path = record.file.path
            text = _extract_text(saved_path, extension)
            record.extracted_text = text
            record.parse_status   = "parsed" if text else "pending"
            record.parsed_at      = timezone.now() if text else None
            record.save(update_fields=["extracted_text", "parse_status", "parsed_at"])
        except Exception as exc:
            logger.exception("Text extraction failed for %s: %s", original_name, exc)
            record.parse_status = "parse_error"
            record.parse_error  = str(exc)
            record.save(update_fields=["parse_status", "parse_error"])

        # 4. Update batch counters
        UploadService._refresh_batch_counters(batch)

        return record

    @staticmethod
    @transaction.atomic
    def ingest_many(batch: UploadBatch, uploaded_files: list, *, user=None) -> list[UploadedFile]:
        """
        Ingest a list of uploaded files into a batch in one call.
        Returns the list of saved UploadedFile records.
        """
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
        files         = batch.files.all()
        total         = files.count()
        parsed        = files.filter(parse_status="parsed").count()
        errors        = files.filter(parse_status="parse_error").count()

        if total == 0:
            new_status = "pending"
        elif errors == total:
            new_status = "failed"
        elif parsed + errors == total:
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