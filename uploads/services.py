# uploads/services.py
"""
UploadService — handles file ingestion into an UploadBatch.

Responsibilities
----------------
1.  Save the uploaded file to media storage (UploadedFile record).
2.  Extract plain text from the file using the `unstructured` library.
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
from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
from django.db import transaction
from django.utils import timezone

from .models import UploadBatch, UploadedFile

logger = logging.getLogger(__name__)

# Max characters stored in extracted_text (keeps DB rows reasonable)
MAX_CHARS = getattr(settings, "UNSTRUCTURED_MAX_CHARS", 50_000)


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text(file_path: str, extension: str) -> str:
    """
    Extract plain text from a file using unstructured.

    Falls back to a simple utf-8 decode for .txt and .csv files so the
    service works even if unstructured is not yet installed.
    """
    # Fast path for plain text — no unstructured needed
    if extension in ("txt", "csv"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[:MAX_CHARS]
        except Exception as exc:
            logger.warning("Plain text read failed for %s: %s", file_path, exc)
            return ""

    # Try unstructured
    try:
        from unstructured.partition.auto import partition

        elements = partition(filename=file_path)
        text = "\n\n".join(str(el) for el in elements if str(el).strip())
        return text[:MAX_CHARS]

    except ImportError:
        logger.warning(
            "unstructured is not installed. "
            "Run: pip install 'unstructured[pdf,xlsx]'  "
            "Only .txt/.csv files will have extracted_text populated."
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

        Usage
        -----
        batch = UploadService.create_batch(
            label="April 2026 URA Receipts",
            user=request.user,
        )
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
        Save one uploaded file into `batch` and extract its text.

        Parameters
        ----------
        batch         UploadBatch to attach the file to.
        uploaded_file Django InMemoryUploadedFile / TemporaryUploadedFile
                      (from request.FILES) or any file-like with a .name attribute.

        Returns
        -------
        UploadedFile  — the saved record with extracted_text populated.
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

        # 2. Extract text from the saved file (read from disk, not from memory)
        try:
            saved_path = record.file.path          # absolute path on disk
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

        # 3. Update batch counters
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
        # Refresh the in-memory instance so callers see the new values
        batch.refresh_from_db()