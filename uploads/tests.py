"""
Regression tests for UploadService extraction dispatch and terminal status.

Covers two bugs where a file (and its batch) could be stranded forever:

  1. extract_file_text_task was dispatched inside the atomic block, so the
     worker could load the row before it committed, get DoesNotExist, and
     give up without retrying.
  2. A successful extraction that yielded no text was written as "pending",
     which is indistinguishable from "still queued" and pins the batch at
     "processing" with nothing left to re-run it.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from organizations.models import Organization
from uploads.models import UploadBatch, UploadedFile
from uploads.services import UploadService


def _make_user(email="uploader@example.com"):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", first_name="Up", last_name="Loader",
    )


class EmptyExtractionIsTerminalTests(TestCase):
    """A completed-but-empty extraction must not look like 'still queued'."""

    def setUp(self):
        self.user = _make_user()
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _ingest(self, extracted):
        upload = SimpleUploadedFile("scan.txt", b"irrelevant", content_type="text/plain")
        with patch("uploads.services._extract_text", return_value=extracted):
            return UploadService.ingest_file(self.batch, upload)

    def test_empty_inline_extraction_is_parsed_not_pending(self):
        record = self._ingest("")
        record.refresh_from_db()
        self.assertEqual(record.parse_status, "parsed")
        self.assertEqual(record.extracted_text, "")
        self.assertIsNotNone(record.parsed_at)

    def test_batch_completes_when_extraction_yields_no_text(self):
        self._ingest("")
        self.batch.refresh_from_db()
        # Previously "processing" forever: pending_or_parsing > 0 with nothing queued.
        self.assertEqual(self.batch.status, "completed")

    def test_non_empty_extraction_still_parses(self):
        record = self._ingest("hello")
        record.refresh_from_db()
        self.assertEqual(record.parse_status, "parsed")
        self.assertEqual(record.extracted_text, "hello")

    def test_empty_background_extraction_is_parsed_not_pending(self):
        record = self._ingest("")
        UploadService.complete_extraction(record.pk, text="")
        record.refresh_from_db()
        self.assertEqual(record.parse_status, "parsed")

    def test_extraction_failure_is_still_parse_error(self):
        upload = SimpleUploadedFile("bad.txt", b"x", content_type="text/plain")
        with patch("uploads.services._extract_text", side_effect=RuntimeError("boom")):
            record = UploadService.ingest_file(self.batch, upload)
        record.refresh_from_db()
        self.assertEqual(record.parse_status, "parse_error")
        self.assertIn("boom", record.parse_error)

    def test_large_extraction_is_not_silently_truncated(self):
        record = UploadedFile.objects.create(
            batch=self.batch,
            original_filename="large.csv",
            file_size_bytes=1,
            extension="csv",
            parse_status="pending",
            extracted_text="",
        )

        long_text = "row," + ("x" * 200_000)
        UploadService.complete_extraction(record.pk, text=long_text)
        record.refresh_from_db()

        self.assertEqual(len(record.extracted_text), len(long_text))
        self.assertEqual(record.parse_status, "parsed")


class DeferredDispatchWaitsForCommitTests(TransactionTestCase):
    """
    The Celery dispatch for a deferred file must happen after commit, so the
    worker can always see the row it is handed.

    TransactionTestCase (not TestCase) so on_commit callbacks actually fire.
    """

    def setUp(self):
        self.user = _make_user("deferred@example.com")
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _ingest_large_pdf(self):
        upload = SimpleUploadedFile("big.pdf", b"%PDF-1.4", content_type="application/pdf")
        # Force the deferred branch without needing a real multi-hundred-page PDF.
        with patch("uploads.services._count_pdf_pages", return_value=10_000), \
             patch("uploads.services._extract_text", return_value="fallback text"):
            return UploadService.ingest_file(self.batch, upload)

    def test_row_is_visible_to_the_worker_when_dispatch_happens(self):
        seen = {}

        def _capture(pk):
            # Emulate the worker: this must find the row, i.e. we are post-commit.
            seen["exists"] = UploadedFile.objects.filter(pk=pk).exists()
            seen["in_atomic"] = transaction.get_connection().in_atomic_block

        with patch("uploads.tasks.extract_file_text_task.delay", side_effect=_capture):
            record = self._ingest_large_pdf()

        self.assertTrue(seen.get("exists"), "worker would have hit DoesNotExist")
        self.assertFalse(seen.get("in_atomic"), "dispatched while still inside the transaction")
        self.assertEqual(record.parse_status, "pending")

    def test_broker_failure_falls_back_inline_and_settles_the_batch(self):
        with patch(
            "uploads.tasks.extract_file_text_task.delay",
            side_effect=OSError("broker unreachable"),
        ):
            record = self._ingest_large_pdf()

        record.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(record.parse_status, "parsed")
        self.assertEqual(record.extracted_text, "fallback text")
        self.assertFalse(record.extraction_deferred, "stale deferred flag on an inline-parsed file")
        self.assertEqual(self.batch.status, "completed", "batch left mid-flight after fallback")

    def tearDown(self):
        UploadedFile.objects.all().delete()
        UploadBatch.objects.all().delete()


class SkippedFilesAreTerminalTests(TestCase):
    """
    _refresh_batch_counters had no branch for parse_status="skipped": such files
    counted toward `total` but toward none of parsed/errors/pending, so the
    "everything finished" test could never pass and the batch stayed
    "processing" forever. Reachable from the admin "Mark as skipped" action.
    """

    def setUp(self):
        self.user = _make_user("skip@example.com")
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _file(self, status):
        return UploadedFile.objects.create(
            batch=self.batch, original_filename=f"{status}.txt",
            file_size_bytes=1, extension="txt", parse_status=status,
        )

    def test_all_skipped_batch_completes(self):
        self._file("skipped")
        UploadService._refresh_batch_counters(self.batch)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "completed")

    def test_skipped_alongside_parsed_completes(self):
        self._file("skipped")
        self._file("parsed")
        UploadService._refresh_batch_counters(self.batch)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "completed")

    def test_skipped_alongside_error_is_partial(self):
        self._file("skipped")
        self._file("parse_error")
        UploadService._refresh_batch_counters(self.batch)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "partial")

    def test_pending_still_keeps_the_batch_processing(self):
        self._file("skipped")
        self._file("pending")
        UploadService._refresh_batch_counters(self.batch)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, "processing")


class ExtractionRetryTests(TestCase):
    """
    The task wrote terminal failure state (parse_error, wiped text, "File
    processing failed" notification) *before* scheduling its retry, so a
    transient blip looked like a hard failure even when the retry succeeded.
    """

    def setUp(self):
        self.user = _make_user("retry@example.com")
        self.batch = UploadService.create_batch(label="t", user=self.user)
        self.record = UploadedFile.objects.create(
            batch=self.batch, original_filename="doc.txt", file_size_bytes=1,
            extension="txt", parse_status="pending", extracted_text="",
        )

    # The record is created with no backing file, so uf.file.path raises inside
    # the task's try block — a stand-in for any transient storage failure.

    def test_transient_failure_does_not_mark_parse_error(self):
        from celery.exceptions import Retry
        from uploads.tasks import extract_file_text_task

        with patch.object(extract_file_text_task, "retry", side_effect=Retry()), \
             self.assertRaises(Retry):
            extract_file_text_task(self.record.pk)

        self.record.refresh_from_db()
        self.assertNotEqual(self.record.parse_status, "parse_error",
                            "terminal failure written before the retry ran")
        self.assertEqual(self.record.parse_error, "")

    def test_exhausted_retries_do_mark_parse_error(self):
        from uploads.tasks import extract_file_text_task

        exc = extract_file_text_task.MaxRetriesExceededError()
        with patch.object(extract_file_text_task, "retry", side_effect=exc):
            res = extract_file_text_task(self.record.pk)

        self.assertFalse(res["ok"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.parse_status, "parse_error")
        self.assertTrue(self.record.parse_error)

    def test_reextract_dispatches_instead_of_calling_inline(self):
        from uploads.tasks import extract_file_text_task, reextract_file_task

        with patch.object(extract_file_text_task, "delay") as delayed:
            delayed.return_value = type("R", (), {"id": "task-1"})()
            res = reextract_file_task(self.record.pk)

        delayed.assert_called_once_with(self.record.pk)
        self.assertEqual(res["task_id"], "task-1")


class SafeDispatchTests(TestCase):
    """
    config.dispatch.dispatch turns a broker outage into a return value.

    dispatch() calls task.apply_async(...) (not task.delay(...)) — see the
    docstring in config/dispatch.py for why (avoiding positional/keyword
    mis-mapping across callers with different task signatures) — so these
    mocks must patch apply_async, the call dispatch() actually makes.
    """

    def test_broker_failure_returns_none_instead_of_raising(self):
        from config.dispatch import dispatch

        task = MagicMock()
        task.name = "some.task"
        task.apply_async.side_effect = OSError("Connection refused")
        self.assertIsNone(dispatch(task, 1, x=2))

    def test_success_returns_the_async_result(self):
        from config.dispatch import dispatch

        task = MagicMock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(dispatch(task, 1), "async-result")
        task.apply_async.assert_called_once_with(args=(1,))

    def test_success_with_kwargs_uses_apply_async_kwargs(self):
        from config.dispatch import dispatch

        task = MagicMock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(dispatch(task, 1, x=2), "async-result")
        task.apply_async.assert_called_once_with(args=(1,), kwargs={"x": 2})

    def test_success_with_only_kwargs_uses_apply_async_kwargs(self):
        from config.dispatch import dispatch

        task = MagicMock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(dispatch(task, x=2), "async-result")
        task.apply_async.assert_called_once_with(kwargs={"x": 2})


class OrgIsolationTests(TestCase):
    """
    Batches/files became org-shared in Phase 3 (previously scoped strictly to
    the uploading user). Verify: (1) a teammate in the same org can see data
    they didn't personally upload — the actual point of org-sharing — and
    (2) a user in a different org, including an admin, gets no visibility at
    all — the admin bypass in _user_owns_or_is_admin is org-bound, not global.
    """

    def setUp(self):
        User = get_user_model()
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        self.uploader_a = User.objects.create_user(
            email="uploader_a@example.com", password="pw12345!",
            first_name="U", last_name="A", role="finance", organization=self.org_a,
        )
        self.teammate_a = User.objects.create_user(
            email="teammate_a@example.com", password="pw12345!",
            first_name="T", last_name="A", role="finance", organization=self.org_a,
        )
        self.admin_b = User.objects.create_user(
            email="admin_b@example.com", password="pw12345!",
            first_name="Ad", last_name="B", role="admin", organization=self.org_b,
        )

        self.batch = UploadService.create_batch(label="Org A batch", user=self.uploader_a)
        upload = SimpleUploadedFile("scan.txt", b"content", content_type="text/plain")
        with patch("uploads.services._extract_text", return_value="hello"):
            self.uf = UploadService.ingest_file(self.batch, upload)

        self.client = APIClient()

    def test_teammate_in_same_org_sees_batch_they_did_not_upload(self):
        self.client.force_authenticate(user=self.teammate_a)
        resp = self.client.get("/api/uploads/batches/")
        self.assertEqual(resp.status_code, 200)
        ids = [b["id"] for b in resp.json()]
        self.assertIn(self.batch.pk, ids)

        detail = self.client.get(f"/api/uploads/batches/{self.batch.pk}/")
        self.assertEqual(detail.status_code, 200)

    def test_cross_org_user_sees_nothing(self):
        self.client.force_authenticate(user=self.admin_b)
        resp = self.client.get("/api/uploads/batches/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

        detail = self.client.get(f"/api/uploads/batches/{self.batch.pk}/")
        self.assertEqual(detail.status_code, 404)

    def test_cross_org_admin_bypass_does_not_apply(self):
        """An admin in a DIFFERENT org must not be able to download/delete a
        file via the owns-or-admin bypass — that bypass is org-bound."""
        self.client.force_authenticate(user=self.admin_b)
        resp = self.client.get(f"/api/uploads/files/{self.uf.pk}/download/")
        self.assertEqual(resp.status_code, 403)

    def test_same_org_admin_bypass_still_applies(self):
        """An admin in the SAME org can still access a teammate's file —
        the pre-existing behavior, just now org-bound instead of global."""
        User = get_user_model()
        admin_a = User.objects.create_user(
            email="admin_a@example.com", password="pw12345!",
            first_name="Ad", last_name="A", role="admin", organization=self.org_a,
        )
        self.client.force_authenticate(user=admin_a)
        resp = self.client.get(f"/api/uploads/files/{self.uf.pk}/download/")
        self.assertEqual(resp.status_code, 200)

    def test_batch_summary_view_is_org_scoped(self):
        self.client.force_authenticate(user=self.teammate_a)
        resp = self.client.get(f"/api/uploads/batches/{self.batch.pk}/summary/")
        self.assertEqual(resp.status_code, 200)

        self.client.force_authenticate(user=self.admin_b)
        resp = self.client.get(f"/api/uploads/batches/{self.batch.pk}/summary/")
        self.assertEqual(resp.status_code, 404)


class PdfIngestionIsNeverTruncatedTests(TestCase):
    """
    _extract_pdf_pages used to silently fall back to a 2,000,000-character
    cap ("MAX_CHARS") whenever a caller passed max_chars=None intending "no
    limit" — which is exactly what the normal ingest path (_extract_text,
    extract_file_text_task) always did. Any PDF whose full text exceeded
    that was permanently truncated in storage with no way for read_file's
    pagination or tools/services.py's chunking to ever recover the missing
    tail, since it was never saved in the first place. This must never
    truncate regardless of document size when max_chars is None.
    """

    def _mock_pdf(self, page_texts):
        pages = []
        for text in page_texts:
            page = MagicMock()
            page.extract_text.return_value = text
            pages.append(page)
        pdf = MagicMock()
        pdf.pages = pages
        pdf.__enter__.return_value = pdf
        pdf.__exit__.return_value = False
        return pdf

    def test_full_document_over_two_million_chars_is_not_truncated(self):
        from uploads.services import _extract_pdf_pages

        # Three pages, each well over 700_000 chars — total > 2_100_000,
        # comfortably past the old silent cap.
        page_texts = ["x" * 700_001, "y" * 700_001, "z" * 700_001]
        fake_pdf = self._mock_pdf(page_texts)

        with patch("pdfplumber.open", return_value=fake_pdf):
            text = _extract_pdf_pages("fake.pdf", page_from=1, page_to=None, max_chars=None)

        self.assertNotIn("truncated at", text)
        for page_text in page_texts:
            self.assertIn(page_text, text)
        self.assertGreater(len(text), 2_100_000)

    def test_explicit_max_chars_still_caps_a_single_call(self):
        """A real per-call cap (e.g. read_file's paginated reads) must still
        work — only the None-means-unbounded ingest path changed."""
        from uploads.services import _extract_pdf_pages

        fake_pdf = self._mock_pdf(["a" * 500])
        with patch("pdfplumber.open", return_value=fake_pdf):
            text = _extract_pdf_pages("fake.pdf", page_from=1, page_to=None, max_chars=100)

        self.assertIn("truncated at 100 characters", text)
