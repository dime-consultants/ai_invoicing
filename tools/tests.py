"""
Regression tests for the read_file tool handler.

read_file used to return {"ok": True, "text": ""} whenever extracted_text was
empty — which is the normal state for a large PDF whose extraction is deferred
to a background worker. The agent is instructed to call read_file immediately
after upload, so it received a *successful* empty read and answered from an
empty document. It also never implemented the page_from/page_to chunked access
that the whole deferred-PDF design documents and depends on.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from tools.handlers import detect_file_type, read_file
from tools.services import _call_prompt_transform
from uploads.models import UploadedFile
from uploads.services import UploadService


class ReadFileTests(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="tools@example.com", password="pw12345!",
            first_name="T", last_name="U",
        )
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _file(self, *, name="doc.pdf", content=b"%PDF-1.4", **kwargs):
        upload = SimpleUploadedFile(name, content)
        defaults = dict(
            batch=self.batch, file=upload, original_filename=name,
            file_size_bytes=len(content), extension=name.rsplit(".", 1)[-1],
            parse_status="parsed", extracted_text="",
        )
        defaults.update(kwargs)
        return UploadedFile.objects.create(**defaults)

    # ── the core regression ───────────────────────────────────────────────────

    def test_missing_file_is_reported(self):
        res = read_file(file_id=999999)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_no_text_layer_is_a_failure_not_an_empty_success(self):
        uf = self._file()
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": True, "text": "", "page_from": 1,
                                 "page_to": 1, "page_count": 1}):
            res = read_file(file_id=uf.pk)
        self.assertFalse(res["ok"], "empty read reported as success")
        self.assertIn("scanned", res["error"].lower())
        self.assertEqual(res["parse_status"], "parsed")

    def test_still_extracting_says_so(self):
        uf = self._file(parse_status="pending", extraction_deferred=True)
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": True, "text": "", "page_from": 1,
                                 "page_to": 1, "page_count": 1}):
            res = read_file(file_id=uf.pk)
        self.assertFalse(res["ok"])
        self.assertIn("still running", res["error"])

    def test_parse_error_is_surfaced(self):
        uf = self._file(parse_status="parse_error", parse_error="pdfplumber blew up")
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": False, "error": "Cannot open PDF: bad header"}):
            res = read_file(file_id=uf.pk)
        self.assertFalse(res["ok"])
        self.assertIn("Cannot open PDF", res["error"])

    # ── deferred large PDFs are readable live ─────────────────────────────────

    def test_deferred_pdf_falls_back_to_live_extract(self):
        uf = self._file(parse_status="pending", extraction_deferred=True, page_count=300)
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": True, "text": "CU 0012345678",
                                 "page_from": 1, "page_to": 300, "page_count": 300}):
            res = read_file(file_id=uf.pk)
        self.assertTrue(res["ok"], "deferred PDF unreadable until the worker finishes")
        self.assertEqual(res["text"], "CU 0012345678")
        self.assertEqual(res["source"], "live_pdf_range")
        self.assertEqual(res["page_count"], 300)

    def test_page_range_is_passed_through_and_bypasses_the_cached_preview(self):
        uf = self._file(extracted_text="whole-document preview", page_count=300)
        with patch("uploads.services.extract_pdf_page_range") as ranged:
            ranged.return_value = {"ok": True, "text": "page 40-60 text",
                                   "page_from": 40, "page_to": 60, "page_count": 300}
            res = read_file(file_id=uf.pk, page_from=40, page_to=60)

        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "page 40-60 text")
        self.assertEqual((res["page_from"], res["page_to"]), (40, 60))
        kwargs = ranged.call_args.kwargs
        self.assertEqual((kwargs["page_from"], kwargs["page_to"]), (40, 60))

    # ── existing behaviour preserved ──────────────────────────────────────────

    def test_cached_text_is_used_when_present(self):
        uf = self._file(extracted_text="already extracted")
        res = read_file(file_id=uf.pk)
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "already extracted")
        self.assertEqual(res["source"], "extracted_text")
        self.assertFalse(res["truncated"])

    def test_txt_raw_fallback_still_works(self):
        uf = self._file(name="notes.txt", content=b"plain text body")
        res = read_file(file_id=uf.pk)
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "plain text body")
        self.assertEqual(res["source"], "raw_file")

    def test_truncation_reports_full_length(self):
        uf = self._file(extracted_text="x" * 500)
        res = read_file(file_id=uf.pk, max_chars=100)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["text"]), 100)
        self.assertEqual(res["full_length"], 500)
        self.assertTrue(res["truncated"])

    def test_handler_never_raises(self):
        uf = self._file(name="report.docx", content=b"not really a docx")
        with patch("uploads.services._extract_text", side_effect=RuntimeError("boom")):
            res = read_file(file_id=uf.pk)
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])


class DetectFileTypeTests(TestCase):
    """
    detect_file_type sniffed extracted_text and *persisted* the verdict. For a
    deferred large PDF that text is empty, so every big bill was permanently
    labelled "generic_pdf" and nothing ever recomputed it.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="detect@example.com", password="pw12345!",
            first_name="D", last_name="T",
        )
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _pdf(self, **kwargs):
        upload = SimpleUploadedFile("bill.pdf", b"%PDF-1.4")
        defaults = dict(
            batch=self.batch, file=upload, original_filename="bill.pdf",
            file_size_bytes=8, extension="pdf", parse_status="pending",
            extracted_text="", extraction_deferred=True, page_count=300,
        )
        defaults.update(kwargs)
        return UploadedFile.objects.create(**defaults)

    def test_deferred_pdf_is_sniffed_live_not_mislabelled(self):
        uf = self._pdf()
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": True, "text": "SAFARICOM POSTPAY TAX INVOICE"}):
            res = detect_file_type(file_id=uf.pk)

        self.assertEqual(res["detected_type"], "safaricom_bill")
        self.assertEqual(res["confidence"], "high")
        self.assertTrue(res["based_on_content"])
        uf.refresh_from_db()
        self.assertEqual(uf.detected_type, "safaricom_bill")

    def test_verdict_from_no_content_is_not_persisted(self):
        uf = self._pdf()
        with patch("uploads.services.extract_pdf_page_range",
                   return_value={"ok": True, "text": ""}):
            res = detect_file_type(file_id=uf.pk)

        self.assertFalse(res["based_on_content"])
        self.assertEqual(res["confidence"], "low")
        uf.refresh_from_db()
        self.assertEqual(uf.detected_type or "", "",
                         "provisional guess persisted as if it were real")

    def test_detection_is_redone_when_extraction_lands(self):
        uf = self._pdf(detected_type="generic_pdf", detection_confidence="low")
        UploadService.complete_extraction(
            uf.pk, text="SAFARICOM POSTPAY BILLED AMOUNT 1,234", page_count=300,
        )
        uf.refresh_from_db()
        self.assertEqual(uf.detected_type, "safaricom_bill",
                         "stale low-confidence verdict never recomputed")
        self.assertEqual(uf.detection_confidence, "high")

    def test_high_confidence_verdict_is_not_clobbered(self):
        uf = self._pdf(detected_type="acon_export", detection_confidence="high")
        UploadService.complete_extraction(uf.pk, text="SAFARICOM POSTPAY", page_count=1)
        uf.refresh_from_db()
        self.assertEqual(uf.detected_type, "acon_export")


class PromptTransformInputTests(TestCase):
    """
    _call_prompt_transform silently capped input at 10 pages / 8 000 chars and
    still returned ok=True, so a reconciliation could be built from ~3% of a
    document with nothing recording that.
    """

    def setUp(self):
        self.config = SimpleNamespace(
            name="extract_safaricom_bill",
            system_prompt="Extract from:\n{file_text}",
            output_schema=None,
        )
        self.user = get_user_model().objects.create_user(
            email="pt@example.com", password="pw12345!", first_name="P", last_name="T",
        )
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _pdf(self, **kwargs):
        upload = SimpleUploadedFile("bill.pdf", b"%PDF-1.4")
        defaults = dict(
            batch=self.batch, file=upload, original_filename="bill.pdf",
            file_size_bytes=8, extension="pdf", parse_status="pending",
            extracted_text="", page_count=300,
        )
        defaults.update(kwargs)
        return UploadedFile.objects.create(**defaults)

    def _run(self, arguments):
        """Call the dispatcher with a stubbed Grok client; return (result, system_prompt)."""
        captured = {}

        def _create(**kwargs):
            captured["system"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="done"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        client = MagicMock()
        client.chat.completions.create.side_effect = _create
        with patch("tools.services._get_grok_client", return_value=client):
            res = _call_prompt_transform(self.config, arguments)
        return res, captured.get("system", "")

    def test_whole_document_is_read_not_the_first_ten_pages(self):
        uf = self._pdf()
        with patch("uploads.services.extract_pdf_page_range") as ranged:
            ranged.return_value = {"ok": True, "text": "body",
                                   "page_from": 1, "page_to": 300, "page_count": 300}
            res, _ = self._run({"file_id": uf.pk})

        self.assertIsNone(ranged.call_args.kwargs["page_to"],
                          "still capping the read at a fixed page count")
        self.assertTrue(res["ok"])
        self.assertEqual(res["page_count"], 300)
        self.assertFalse(res["input_truncated"])

    def test_explicit_page_range_is_honoured(self):
        uf = self._pdf()
        with patch("uploads.services.extract_pdf_page_range") as ranged:
            ranged.return_value = {"ok": True, "text": "body",
                                   "page_from": 40, "page_to": 60, "page_count": 300}
            self._run({"file_id": uf.pk, "page_from": 40, "page_to": 60})

        kwargs = ranged.call_args.kwargs
        self.assertEqual((kwargs["page_from"], kwargs["page_to"]), (40, 60))

    @override_settings(PROMPT_TRANSFORM_MAX_CHARS=100)
    def test_oversized_single_source_is_chunked_and_merged_not_truncated(self):
        """
        A single-source input over the cap is no longer silently truncated —
        it's split into <=max_chars pieces, each analyzed, then merged into
        one result covering the whole document (see _run_chunked_prompt_transform).
        """
        uf = self._pdf(extracted_text="x" * 5000)
        res, _ = self._run({"file_id": uf.pk})

        self.assertTrue(res["ok"])
        self.assertFalse(res["input_truncated"], "chunk+merge covers the whole document — nothing was dropped")
        self.assertEqual(res["input_full_length"], 5000)
        self.assertTrue(res.get("chunked"))
        self.assertEqual(res["chunk_count"], 50)  # 5000 chars / 100-char cap
        self.assertEqual(res["result"], "done")

    def test_untruncated_input_is_not_flagged(self):
        uf = self._pdf(extracted_text="short body")
        res, system_prompt = self._run({"file_id": uf.pk})
        self.assertFalse(res["input_truncated"])
        self.assertNotIn("TRUNCATED", system_prompt)
        self.assertIn("short body", system_prompt)


class MultiFileResolutionTests(TestCase):
    """
    Only `file_id` was ever resolved, so every multi-file tool received an empty
    {file_text} and answered from nothing: reconcile_datasets (file_id_a /
    file_id_b) and summarise_batch (batch_id) were both non-functional.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="multi@example.com", password="pw12345!", first_name="M", last_name="F",
        )
        self.batch = UploadService.create_batch(label="t", user=self.user)

    def _file(self, text, name="f.txt"):
        return UploadedFile.objects.create(
            batch=self.batch, file=SimpleUploadedFile(name, b"x"),
            original_filename=name, file_size_bytes=1,
            extension=name.rsplit(".", 1)[-1], parse_status="parsed",
            extracted_text=text,
        )

    def _run(self, config, arguments):
        captured = {}

        def _create(**kwargs):
            captured["system"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="done"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        client = MagicMock()
        client.chat.completions.create.side_effect = _create
        with patch("tools.services._get_grok_client", return_value=client):
            res = _call_prompt_transform(config, arguments)
        return res, captured.get("system", "")

    def test_both_sides_of_a_reconciliation_are_loaded(self):
        a = self._file("URA SIDE CU-111", "ura.txt")
        b = self._file("ACON SIDE CU-111", "acon.txt")
        cfg = SimpleNamespace(
            name="reconcile_datasets", output_schema=None,
            system_prompt="A:\n{file_a_text}\nB:\n{file_b_text}",
        )
        res, prompt = self._run(cfg, {"file_id_a": a.pk, "file_id_b": b.pk})

        self.assertIn("URA SIDE CU-111", prompt)
        self.assertIn("ACON SIDE CU-111", prompt, "file B never reached the model")
        self.assertEqual(res["input_full_length"], len("URA SIDE CU-111") + len("ACON SIDE CU-111"))

    def test_batch_id_loads_every_file_labelled(self):
        self._file("first doc", "a.txt")
        self._file("second doc", "b.txt")
        cfg = SimpleNamespace(
            name="summarise_batch", output_schema=None,
            system_prompt="Batch:\n{batch_text}",
        )
        _, prompt = self._run(cfg, {"batch_id": self.batch.pk})

        self.assertIn("first doc", prompt)
        self.assertIn("second doc", prompt)
        self.assertIn("a.txt", prompt, "files are not labelled in the batch blob")

    def test_unreferenced_placeholders_do_not_leak_into_the_prompt(self):
        a = self._file("only A", "a.txt")
        cfg = SimpleNamespace(
            name="reconcile_datasets", output_schema=None,
            system_prompt="A:\n{file_a_text}\nB:\n{file_b_text}",
        )
        _, prompt = self._run(cfg, {"file_id_a": a.pk})
        self.assertNotIn("{file_b_text}", prompt,
                         "literal placeholder reached the model as content")

    @override_settings(PROMPT_TRANSFORM_MAX_CHARS=100)
    def test_budget_is_split_across_sources(self):
        a = self._file("a" * 500, "a.txt")
        b = self._file("b" * 500, "b.txt")
        cfg = SimpleNamespace(
            name="reconcile_datasets", output_schema=None,
            system_prompt="A:\n{file_a_text}\nB:\n{file_b_text}",
        )
        res, prompt = self._run(cfg, {"file_id_a": a.pk, "file_id_b": b.pk})

        self.assertTrue(res["input_truncated"])
        self.assertEqual(res["input_full_length"], 1000)
        # 100 total across 2 sources = 50 each, not 100 each.
        self.assertEqual(prompt.count("a" * 50), 1)
        self.assertNotIn("a" * 51, prompt)

    def test_single_file_contract_still_works(self):
        f = self._file("legacy body", "a.txt")
        cfg = SimpleNamespace(
            name="extract_invoice_data", output_schema=None,
            system_prompt="Doc:\n{file_text}",
        )
        _, prompt = self._run(cfg, {"file_id": f.pk})
        self.assertIn("legacy body", prompt)

    def test_file_text_falls_back_to_side_a_for_legacy_prompts(self):
        a = self._file("side A body", "a.txt")
        b = self._file("side B body", "b.txt")
        cfg = SimpleNamespace(
            name="reconcile_datasets", output_schema=None,
            system_prompt="Doc:\n{file_text}",
        )
        _, prompt = self._run(cfg, {"file_id_a": a.pk, "file_id_b": b.pk})
        self.assertIn("side A body", prompt)


class RestoredDomainHandlerTests(TestCase):
    """
    The CU-matching logic these cover is exactly what the generic
    prompt_transform replacements could not do:
      - URA/ACON join on the fiscal/statutory column, NOT ACON's Item Number
      - identifier normalisation so Excel's '12345.0' matches '12345'
      - URA number format '4 862 563,00' (space thousands, comma decimal)
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="domain@example.com", password="pw12345!", first_name="D", last_name="H",
        )
        self.batch = UploadService.create_batch(label="t", user=self.user)

    # ── normalisation primitives ──────────────────────────────────────────────

    def test_norm_id_strips_excel_float_artefact(self):
        from tools.handlers import _norm_id
        self.assertEqual(_norm_id("0012345678.0"), "0012345678")
        self.assertEqual(_norm_id(" 12345 "), "12345")
        self.assertEqual(_norm_id(12345), "12345")

    def test_norm_number_handles_ura_and_standard_formats(self):
        from tools.handlers import _norm_number
        self.assertEqual(_norm_number("4 862 563,00"), 4862563.0)   # URA style
        self.assertEqual(_norm_number("1,234.56"), 1234.56)         # standard
        self.assertEqual(_norm_number("1000"), 1000.0)

    def test_saf_department_strips_company_prefix(self):
        from tools.handlers import _saf_department
        self.assertEqual(_saf_department("Kuehne + Nagel Ltd - Finance"), "Finance")
        self.assertEqual(_saf_department("Kuehne and Nagel Airfreight"), "Airfreight")
        self.assertEqual(_saf_department("Standalone Name"), "Standalone Name")

    # ── the URA/ACON join ─────────────────────────────────────────────────────

    def _xlsx(self, name, headers, rows):
        import openpyxl, io
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return UploadedFile.objects.create(
            batch=self.batch, file=SimpleUploadedFile(name, buf.read()),
            original_filename=name, file_size_bytes=1, extension="xlsx",
            parse_status="parsed",
        )

    def test_reconciliation_joins_on_statutory_column_not_item_number(self):
        from tools.handlers import reconcile_ura_vs_acon

        ura = self._xlsx(
            "ura.xlsx",
            ["FDN", "Name of Purchaser", "Invoice Date", "TOTAL (A+B)", "VAT Charged"],
            [["0012345678", "ACME LTD", "01-08-2026", 1000.0, 100.0]],
        )
        # Item Number is a decoy: it must NOT be used as the join key.
        acon = self._xlsx(
            "acon.xlsx",
            ["Debtor Account", "Full Name", "Item Number",
             "Statutory Item No(For Download VAT)", "LC Amount"],
            [["D1", "ACME LTD", "999999", "0012345678", 1000.0]],
        )

        res = reconcile_ura_vs_acon(ura_file_id=ura.pk, acon_file_id=acon.pk)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["matched_count"], 1, "joined on the wrong column")
        self.assertEqual(res["missing_in_acon"], 0)
        self.assertEqual(res["variance_count"], 0)
        self.assertEqual(res["rows"][0]["acon_item"], "999999")

    def test_excel_float_artefact_still_matches(self):
        from tools.handlers import reconcile_ura_vs_acon
        ura  = self._xlsx("ura.xlsx",
                          ["FDN", "Name of Purchaser", "Invoice Date", "TOTAL (A+B)"],
                          [["12345", "ACME", "01-08-2026", 500.0]])
        acon = self._xlsx("acon.xlsx",
                          ["Statutory Item No(For Download VAT)", "Full Name", "LC Amount"],
                          [[12345.0, "ACME", 500.0]])   # Excel numeric cell -> '12345.0'

        res = reconcile_ura_vs_acon(ura_file_id=ura.pk, acon_file_id=acon.pk)
        self.assertEqual(res["matched_count"], 1,
                         "'12345.0' vs '12345' failed to match — _norm_id not applied")

    def test_variance_and_missing_rows_are_classified(self):
        from tools.handlers import reconcile_ura_vs_acon
        ura = self._xlsx("ura.xlsx",
                         ["FDN", "Name of Purchaser", "Invoice Date", "TOTAL (A+B)"],
                         [["111", "A", "01-08-2026", 1000.0],
                          ["222", "B", "01-08-2026", 2000.0],
                          ["333", "C", "01-08-2026", 3000.0]])
        acon = self._xlsx("acon.xlsx",
                          ["Statutory Item No(For Download VAT)", "Full Name", "LC Amount"],
                          [["111", "A", 1000.0],      # match
                           ["222", "B", 1900.0],      # variance
                           ["444", "D", 4000.0]])     # missing in URA

        res = reconcile_ura_vs_acon(ura_file_id=ura.pk, acon_file_id=acon.pk)
        self.assertEqual(res["matched_count"], 2)
        self.assertEqual(res["variance_count"], 1)
        self.assertEqual(res["missing_in_acon"], 1)   # 333
        self.assertEqual(res["missing_in_ura"], 1)    # 444

    def test_workpaper_is_written_and_collectable(self):
        from pathlib import Path
        from tools.handlers import reconcile_ura_vs_acon
        ura  = self._xlsx("ura.xlsx",
                          ["FDN", "Name of Purchaser", "Invoice Date", "TOTAL (A+B)"],
                          [["111", "A", "01-08-2026", 10.0]])
        acon = self._xlsx("acon.xlsx",
                          ["Statutory Item No(For Download VAT)", "Full Name", "LC Amount"],
                          [["111", "A", 10.0]])
        res = reconcile_ura_vs_acon(ura_file_id=ura.pk, acon_file_id=acon.pk)
        # collect_output_files reads result["output_filename"] as an absolute path.
        self.assertTrue(Path(res["output_filename"]).exists())

    def test_txt_fiscal_side_parses_cu_blocks(self):
        from tools.handlers import _parse_fiscal_side
        raw = (
            "FISCAL RECEIPT\r\n"
            "CU INVOICE NUMBER: 0012345678\r\n"
            "01-08-2026 10:30:00\r\n"
            "TOTAL:   4 862 563,00\r\n"
            "TAXES:   662 563,00\r\n"
            "CREDIT NOTE\r\n"
            "CU INVOICE NUMBER: 0087654321\r\n"
            "02-08-2026 11:00:00\r\n"
            "TOTAL:   1 000,00\r\n"
            "TAXES:   100,00\r\n"
        )
        uf = UploadedFile.objects.create(
            batch=self.batch, file=SimpleUploadedFile("ura.txt", raw.encode()),
            original_filename="ura.txt", file_size_bytes=len(raw), extension="txt",
            parse_status="parsed",
        )
        recs = _parse_fiscal_side(uf)
        self.assertEqual(len(recs), 2, "credit notes or CU blocks not parsed")
        self.assertEqual(recs[0]["fiscal_no"], "0012345678")
        self.assertEqual(recs[0]["total"], 4862563.0)
