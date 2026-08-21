"""
UploadService — persistence and file-state operations for uploaded files.

Architecture
------------

HTTP
  ↓
UploadService.receive_file()
  ↓
pipeline_ingest_task
  ↓
extract_file_text_task

Important:
-----------
UploadService NEVER performs text extraction during an HTTP request.

All files — regardless of size, extension, page count, or complexity —
must go through Celery.

The service layer is responsible for:

- creating batches
- persisting uploaded files
- providing extraction helpers used by Celery/tools
- updating extraction state
- refreshing batch counters
- providing PDF page-range extraction for read_file/tool usage

The Celery task layer is responsible for:

- checksum calculation
- MIME detection
- PDF page counting
- extraction orchestration
- extraction progress
- retries
- timeouts
- completion/failure events
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import UploadBatch, UploadedFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PDF_EXTRACTION_CHUNK_SIZE = 50

SPREADSHEET_CHECKPOINT_ROWS = 5000


# ===========================================================================
# Text extraction helpers
# ===========================================================================

def _extract_text(
    file_path: str,
    extension: str,
    *,
    max_chars: int | None = None,
    on_chunk=None,
) -> str:
    """
    Extract text from a file.

    This function does NOT run during HTTP upload processing.

    It is intended to be called by:
        extract_file_text_task
        read_file / other background tooling

    Args:
        file_path:
            Absolute path to the stored file.

        extension:
            Lowercase file extension without ".".

        max_chars:
            Optional character limit.

        on_chunk:
            Optional callback receiving text accumulated so far.

    Returns:
        Extracted text.
    """

    limit = max_chars if max_chars is not None else None

    # ------------------------------------------------------------------
    # TXT / CSV
    # ------------------------------------------------------------------

    if extension in ("txt", "csv"):
        try:
            with open(
                file_path,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as fh:
                text = fh.read()

            if limit is not None:
                return text[:limit]

            return text

        except Exception as exc:
            logger.warning(
                "Plain text read failed for %s: %s",
                file_path,
                exc,
            )
            return ""

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    if extension == "pdf":
        return _extract_pdf_pages(
            file_path,
            page_from=1,
            page_to=None,
            max_chars=limit,
            on_chunk=on_chunk,
        )

    # ------------------------------------------------------------------
    # XLSX / XLS
    # ------------------------------------------------------------------

    if extension in ("xlsx", "xls"):

        checkpoint_every = SPREADSHEET_CHECKPOINT_ROWS

        try:
            parts: list[str] = []
            total = 0
            row_num = 0

            def _maybe_checkpoint() -> None:
                if (
                    on_chunk is not None
                    and row_num % checkpoint_every == 0
                ):
                    try:
                        on_chunk("\n".join(parts))
                    except Exception:
                        logger.warning(
                            "_extract_text: "
                            "on_chunk checkpoint failed",
                            exc_info=True,
                        )

            # ----------------------------------------------------------
            # XLSX
            # ----------------------------------------------------------

            if extension == "xlsx":

                import openpyxl

                wb = openpyxl.load_workbook(
                    file_path,
                    data_only=True,
                    read_only=True,
                )

                try:
                    for ws in wb.worksheets:

                        for row in ws.iter_rows(
                            values_only=True
                        ):
                            row_num += 1

                            line = "\t".join(
                                ""
                                if cell is None
                                else str(cell)
                                for cell in row
                            )

                            if line.strip():
                                parts.append(line)
                                total += len(line)

                            _maybe_checkpoint()

                            if (
                                limit is not None
                                and total >= limit
                            ):
                                break

                        if (
                            limit is not None
                            and total >= limit
                        ):
                            break

                finally:
                    wb.close()

            # ----------------------------------------------------------
            # XLS
            # ----------------------------------------------------------

            else:

                import xlrd

                book = xlrd.open_workbook(file_path)

                for sheet in book.sheets():

                    for row_index in range(sheet.nrows):

                        row_num += 1

                        line = "\t".join(
                            str(cell)
                            for cell in sheet.row_values(row_index)
                        )

                        if line.strip():
                            parts.append(line)
                            total += len(line)

                        _maybe_checkpoint()

                        if (
                            limit is not None
                            and total >= limit
                        ):
                            break

                    if (
                        limit is not None
                        and total >= limit
                    ):
                        break

            text = "\n".join(parts)

            if limit is not None:
                return text[:limit]

            return text

        except Exception as exc:
            logger.warning(
                "Spreadsheet extraction failed for %s: %s",
                file_path,
                exc,
            )
            return ""

    # ------------------------------------------------------------------
    # Other supported formats through unstructured
    # ------------------------------------------------------------------

    try:

        from unstructured.partition.auto import partition

        elements = partition(
            filename=file_path
        )

        text = "\n\n".join(
            str(element)
            for element in elements
            if str(element).strip()
        )

        if limit is not None:
            return text[:limit]

        return text

    except ImportError:

        logger.warning(
            "unstructured is not installed; "
            "'%s' files have no extracted_text. "
            "Install with: "
            "pip install 'unstructured[all-docs]'",
            extension,
        )

        return ""

    except Exception as exc:

        logger.warning(
            "unstructured extraction failed for %s: %s",
            file_path,
            exc,
        )

        return ""


# ===========================================================================
# PDF extraction
# ===========================================================================

def _extract_pdf_pages(
    file_path: str,
    *,
    page_from: int = 1,
    page_to: int | None = None,
    max_chars: int | None = None,
    on_chunk=None,
) -> str:
    """
    Extract text from a PDF page range.

    Page numbers are 1-indexed and inclusive.

    This function supports incremental extraction through `on_chunk`.
    """

    limit = max_chars

    try:

        import pdfplumber

        parts: list[str] = []
        total = 0

        with pdfplumber.open(file_path) as pdf:

            page_count = len(pdf.pages)

            start = max(1, page_from) - 1

            end = (
                page_count
                if page_to is None
                else min(page_to, page_count)
            )

            chunk_size = max(
                1,
                PDF_EXTRACTION_CHUNK_SIZE,
            )

            for chunk_start in range(
                start,
                end,
                chunk_size,
            ):

                chunk_end = min(
                    chunk_start + chunk_size,
                    end,
                )

                for index in range(
                    chunk_start,
                    chunk_end,
                ):

                    page = pdf.pages[index]

                    text = page.extract_text() or ""

                    if text:

                        header = (
                            f"--- Page {index + 1} ---\n"
                        )

                        parts.append(
                            header + text
                        )

                        total += (
                            len(header)
                            + len(text)
                        )

                    page.flush_cache()

                    if (
                        limit is not None
                        and total >= limit
                    ):
                        parts.append(
                            f"\n[… truncated at "
                            f"{limit} characters …]"
                        )
                        break

                # Notify caller after every page chunk.
                if on_chunk is not None:

                    try:
                        on_chunk(
                            "\n\n".join(parts)
                        )

                    except Exception:
                        logger.warning(
                            "_extract_pdf_pages: "
                            "on_chunk checkpoint failed",
                            exc_info=True,
                        )

                if (
                    limit is not None
                    and total >= limit
                ):
                    break

        return "\n\n".join(parts)

    except Exception as exc:

        logger.warning(
            "pdfplumber range extract failed for %s: %s",
            file_path,
            exc,
        )

        return ""


# ===========================================================================
# Public PDF range helper
# ===========================================================================

def extract_pdf_page_range(
    file_path: str,
    page_from: int = 1,
    page_to: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """
    Extract a specific PDF page range.

    Used by read_file and similar tools.

    This is intentionally independent of the background ingestion pipeline.
    It allows targeted page reads without changing the upload architecture.
    """

    try:

        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)

    except Exception as exc:

        return {
            "ok": False,
            "error": f"Cannot open PDF: {exc}",
        }

    start = max(
        1,
        page_from,
    )

    end = (
        page_count
        if page_to is None
        else min(
            max(page_to, start),
            page_count,
        )
    )

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


# ===========================================================================
# UploadService
# ===========================================================================

class UploadService:
    """
    Persistence/state service for uploads.

    IMPORTANT:

    This class does NOT decide whether a file is small or large.

    It does NOT extract text.

    It does NOT queue extraction tasks.

    All extraction is performed by Celery.
    """

    # ------------------------------------------------------------------
    # Batch creation
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def create_batch(
        *,
        label: str,
        description: str = "",
        user,
    ) -> UploadBatch:
        """
        Create a new UploadBatch owned by `user`.
        """

        return UploadBatch.objects.create(
            label=label,
            description=description,
            uploaded_by=user,
            organization=getattr(
                user,
                "organization",
                None,
            ),
            status="pending",
        )

    # ------------------------------------------------------------------
    # File persistence
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def receive_file(
        batch: UploadBatch,
        uploaded,
        detected_type_hint: str = "",
    ) -> UploadedFile:
        """
        Persist an uploaded file.

        This is the ONLY upload operation performed synchronously
        from the HTTP request.

        Responsibilities:

        1. Sanitize filename.
        2. Determine extension.
        3. Create UploadedFile.
        4. Persist raw bytes.
        5. Leave processing to pipeline_ingest_task.

        No:

        - checksum
        - PDF page counting
        - text extraction
        - MIME inspection requiring file parsing
        - Celery dispatch
        - AI processing

        is performed here.
        """

        original_name = Path(
            uploaded.name
        ).name

        extension = (
            Path(original_name)
            .suffix
            .lstrip(".")
            .lower()
        )

        # MIME is only a lightweight filename-based hint.
        # pipeline_ingest_task performs the authoritative metadata step.
        mime_type = ""

        uf = UploadedFile.objects.create(
            batch=batch,
            original_filename=original_name,
            file_size_bytes=getattr(
                uploaded,
                "size",
                0,
            ) or 0,
            mime_type=mime_type,
            extension=extension,
            parse_status="received",
            parse_error="",
            extracted_text="",
        )

        # Persist bytes into the model's configured storage.
        #
        # We explicitly write chunks rather than loading the complete
        # upload into memory.
        with open(
            uf.file.path,
            "wb",
        ) as destination:

            for chunk in uploaded.chunks():
                destination.write(chunk)

        return uf

    # ------------------------------------------------------------------
    # Batch/file queries
    # ------------------------------------------------------------------

    @staticmethod
    def get_batch_files(
        batch: UploadBatch,
    ) -> list[UploadedFile]:
        """
        Return all files belonging to a batch.
        """

        return list(
            batch.files.order_by(
                "uploaded_at"
            )
        )

    # ------------------------------------------------------------------
    # Batch counters
    # ------------------------------------------------------------------

    @staticmethod
    def _refresh_batch_counters(
        batch: UploadBatch,
    ) -> None:
        """
        Recalculate batch counters and status.

        Called by Celery as individual files move through:

            received
              ↓
            pending
              ↓
            parsing
              ↓
            parsed / parse_error
        """

        files = batch.files.all()

        total = files.count()

        parsed = files.filter(
            parse_status="parsed"
        ).count()

        errors = files.filter(
            parse_status="parse_error"
        ).count()

        skipped = files.filter(
            parse_status="skipped"
        ).count()

        pending_or_parsing = files.filter(
            parse_status__in=(
                "received",
                "pending",
                "parsing",
            )
        ).count()

        if total == 0:

            new_status = "pending"

        elif errors == total:

            new_status = "failed"

        elif pending_or_parsing > 0:

            new_status = "processing"

        elif (
            parsed
            + errors
            + skipped
            == total
        ):

            new_status = (
                "completed"
                if errors == 0
                else "partial"
            )

        else:

            new_status = "processing"

        UploadBatch.objects.filter(
            pk=batch.pk
        ).update(
            file_count=total,
            processed_count=parsed,
            error_count=errors,
            status=new_status,
        )

        batch.refresh_from_db()

    # ------------------------------------------------------------------
    # Extraction state: parsing
    # ------------------------------------------------------------------

    @staticmethod
    def mark_parsing(
        file_id: int,
    ) -> UploadedFile | None:
        """
        Mark a file as actively being parsed.

        Called by extract_file_text_task.
        """

        try:

            uf = (
                UploadedFile.objects
                .select_related("batch")
                .get(pk=file_id)
            )

        except UploadedFile.DoesNotExist:

            return None

        uf.parse_status = "parsing"
        uf.parse_error = ""

        uf.save(
            update_fields=[
                "parse_status",
                "parse_error",
            ]
        )

        UploadService._refresh_batch_counters(
            uf.batch
        )

        return uf

    # ------------------------------------------------------------------
    # Extraction state: completion/failure
    # ------------------------------------------------------------------

    @staticmethod
    def complete_extraction(
        file_id: int,
        *,
        text: str,
        page_count: int | None = None,
        error: str | None = None,
    ) -> UploadedFile | None:
        """
        Complete an extraction operation.

        Called by extract_file_text_task.

        Handles both:

            successful extraction
            failed extraction
        """

        try:

            uf = (
                UploadedFile.objects
                .select_related("batch")
                .get(pk=file_id)
            )

        except UploadedFile.DoesNotExist:

            return None

        if error:

            uf.parse_status = "parse_error"
            uf.parse_error = error[:2000]

            # For terminal failures, do not present stale text
            # as successfully extracted content.
            uf.extracted_text = ""

        else:

            uf.extracted_text = text or ""

            uf.parse_status = "parsed"

            uf.parse_error = ""

            uf.parsed_at = timezone.now()

            if page_count is not None:
                uf.page_count = page_count

        update_fields = [
            "extracted_text",
            "parse_status",
            "parse_error",
            "parsed_at",
        ]

        if page_count is not None:
            update_fields.append(
                "page_count"
            )

        uf.save(
            update_fields=update_fields
        )

        # Re-run file-type detection after extraction when appropriate.
        #
        # Extraction may have been unavailable when an earlier detection
        # operation ran. Do not overwrite a high-confidence/manual verdict.
        if (
            not error
            and text
            and uf.detection_confidence
            in (None, "", "low")
        ):

            try:

                from tools.handlers import detect_file_type

                detect_file_type(
                    uf.pk
                )

                uf.refresh_from_db()

            except Exception as exc:

                logger.warning(
                    "Re-detection after extraction failed "
                    "for file %s: %s",
                    uf.pk,
                    exc,
                )

        UploadService._refresh_batch_counters(
            uf.batch
        )

        return uf