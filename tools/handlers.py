# tools/handlers.py
"""
Tool handler functions.

Each function here is the Python implementation of one ToolDefinition.
The handler field in ToolDefinition points to one of these using a
dotted path, e.g. "tools.handlers.extract_ura_receipts".

Rules
-----
- Every handler receives **kwargs matching its ToolDefinition.parameters_schema.
- Every handler returns a plain JSON-serialisable dict.
- Handlers never raise — they catch all exceptions and return
  {"ok": False, "error": "<message>"} so the LLM can report the failure.
- Heavy imports (openpyxl, pdfplumber, unstructured) are deferred inside
  each function so the module loads fast.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _norm_number(raw: str) -> float:
    """
    Normalise ambiguous number strings to float.
    Handles URA style: '4 862 563,00'  →  4862563.0
    Handles standard:  '1,234.56'      →  1234.56
    """
    s = str(raw).strip()
    if re.match(r'^[\d ]+,\d{1,2}$', s):          # space-thousands + comma-decimal
        return float(s.replace(' ', '').replace(',', '.'))
    return float(s.replace(',', '').replace(' ', ''))


def _get_uploaded_file(file_id: int):
    """Return an UploadedFile instance or raise ValueError."""
    from uploads.models import UploadedFile
    try:
        return UploadedFile.objects.get(pk=file_id)
    except UploadedFile.DoesNotExist:
        raise ValueError(f"UploadedFile id={file_id} not found.")


def _save_xlsx(wb, filename: str) -> BytesIO:
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# 1. detect_file_type
# ─────────────────────────────────────────────────────────────────────────────

def detect_file_type(file_id: int) -> dict:
    """
    Inspect an uploaded file and set detected_type + detection_confidence.

    Uses the file extension and a quick content sniff to decide between:
    ura_fiscal_receipt | safaricom_bill | acon_export | generic_xlsx |
    generic_csv | generic_pdf | unknown
    """
    try:
        uf = _get_uploaded_file(file_id)
        ext = uf.extension.lower()
        text = (uf.extracted_text or "")[:2000].upper()

        detected  = "unknown"
        confidence = "low"

        if ext == "txt":
            if "CU INVOICE NUMBER" in text and ("FISCAL RECEIPT" in text or "CREDIT NOTE" in text):
                detected, confidence = "ura_fiscal_receipt", "high"
            elif "PERIODICAL REPORT" in text:
                detected, confidence = "ura_fiscal_receipt", "medium"
            else:
                detected, confidence = "unknown_txt", "low"

        elif ext == "pdf":
            if "SAFARICOM" in text or "BILLED AMOUNT" in text:
                detected, confidence = "safaricom_bill", "high"
            else:
                detected, confidence = "generic_pdf", "low"

        elif ext in ("xlsx", "xls"):
            if "DEBTOR" in text or "ACON" in text or "LC AMOUNT" in text:
                detected, confidence = "acon_export", "high"
            elif "CU INVOICE" in text:
                detected, confidence = "ura_fiscal_receipt", "medium"
            else:
                detected, confidence = "generic_xlsx", "medium"

        elif ext == "csv":
            detected, confidence = "generic_csv", "medium"

        uf.detected_type         = detected
        uf.detection_confidence  = confidence
        uf.save(update_fields=["detected_type", "detection_confidence"])

        return {
            "ok":          True,
            "file_id":     file_id,
            "filename":    uf.original_filename,
            "detected_type": detected,
            "confidence":  confidence,
        }

    except Exception as exc:
        logger.exception("detect_file_type(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 2. extract_ura_receipts
# ─────────────────────────────────────────────────────────────────────────────

def extract_ura_receipts(file_id: int) -> dict:
    """
    Parse a URA fiscal receipt .txt file.

    Extracts every FISCAL RECEIPT / CREDIT NOTE block and returns
    headers + rows.  Also writes an .xlsx output file to
    outputs/converted/ and updates UploadedFile.parse_status.

    Returns:
        {
            "ok": true,
            "record_count": 724,
            "headers": [...],
            "rows": [[...], ...],        # first 5 rows for LLM preview
            "output_filename": "...",
            "summary": "..."
        }
    """
    try:
        import openpyxl
        from django.conf import settings
        from django.utils import timezone

        uf = _get_uploaded_file(file_id)
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])

        uf.file.open("rb")
        raw = uf.file.read().decode("utf-8", errors="replace")

        BLOCK_RE = re.compile(
            r'^(?P<entry_type>FISCAL RECEIPT|CREDIT NOTE)\s*\r?\n'
            r'CU INVOICE NUMBER:\s*(?P<cu_no>\S+)\s*\r?\n'
            r'(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s*\r?\n'
            r'TOTAL:\s+(?P<total>[\d\s,]+)\r?\n'
            r'TAXES:\s+(?P<taxes>[\d\s,]+)',
            re.MULTILINE | re.IGNORECASE,
        )

        headers = ["CU Invoice Number", "Date", "Time", "Total (UGX)", "Taxes (UGX)", "Entry Type"]
        rows = []

        for m in BLOCK_RE.finditer(raw):
            rows.append([
                m.group("cu_no").strip(),
                m.group("date").strip(),
                m.group("time").strip(),
                _norm_number(m.group("total")),
                _norm_number(m.group("taxes")),
                m.group("entry_type").strip().title(),
            ])

        # Build xlsx
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "URA Receipts"
        ws.append(headers)
        for row in rows:
            ws.append(row)

        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(uf.original_filename).stem
        out_path = out_dir / f"{stem}_extracted.xlsx"
        wb.save(out_path)

        uf.parse_status = "parsed"
        uf.parsed_at    = timezone.now()
        uf.save(update_fields=["parse_status", "parsed_at"])

        return {
            "ok":              True,
            "file_id":         file_id,
            "record_count":    len(rows),
            "headers":         headers,
            "rows":            rows[:5],     # preview — full data is in the xlsx
            "output_filename": str(out_path),
            "summary": (
                f"Extracted {len(rows)} receipt records from '{uf.original_filename}'. "
                f"Date range: {rows[0][1] if rows else 'N/A'} – {rows[-1][1] if rows else 'N/A'}. "
                f"Output saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("extract_ura_receipts(%s): %s", file_id, exc)
        try:
            uf = _get_uploaded_file(file_id)
            uf.parse_status = "parse_error"
            uf.parse_error  = str(exc)
            uf.save(update_fields=["parse_status", "parse_error"])
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 3. extract_safaricom_bill
# ─────────────────────────────────────────────────────────────────────────────

def extract_safaricom_bill(file_id: int) -> dict:
    """
    Extract line items from a Safaricom monthly bill PDF.

    Parses tables from every page using pdfplumber and returns
    Name, Reference NO., Invoice NO., Net Amount, VAT, Excise, Billed Amount.
    """
    try:
        import pdfplumber
        import openpyxl
        from django.conf import settings
        from django.utils import timezone

        uf = _get_uploaded_file(file_id)
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])

        headers = ["Name", "Reference NO.", "Invoice NO.", "Net Amount", "VAT", "Excise", "Billed Amount"]
        rows = []

        uf.file.open("rb")
        with pdfplumber.open(uf.file) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    for row in table:
                        if not row or not any(row):
                            continue
                        first = str(row[0] or "").strip()
                        if not first or any(h.upper() in first.upper() for h in ("NAME", "REFERENCE", "INVOICE")):
                            continue
                        if len(row) < 7:
                            continue
                        rows.append([str(c or "").strip() for c in row[:7]])

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Safaricom Bill"
        ws.append(headers)
        for row in rows:
            ws.append(row)

        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(uf.original_filename).stem
        out_path = out_dir / f"{stem}_extracted.xlsx"
        wb.save(out_path)

        uf.parse_status = "parsed"
        uf.parsed_at    = timezone.now()
        uf.save(update_fields=["parse_status", "parsed_at"])

        return {
            "ok":              True,
            "file_id":         file_id,
            "record_count":    len(rows),
            "headers":         headers,
            "rows":            rows[:5],
            "output_filename": str(out_path),
            "summary": (
                f"Extracted {len(rows)} line items from Safaricom bill '{uf.original_filename}'. "
                f"Output saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("extract_safaricom_bill(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 4. clean_acon_export
# ─────────────────────────────────────────────────────────────────────────────

def clean_acon_export(file_id: int) -> dict:
    """
    Load an ACON .xlsx export, normalise headers and values,
    strip empty rows, and return a cleaned dataset.

    Expected columns (partial match, case-insensitive):
    debtor/account, name, abbreviation, class, vat, type,
    item number/invoice, date, currency, lc amount, fc amount,
    vatable, non vatable, vat lc, vat fc, reference.
    """
    try:
        import openpyxl
        from django.conf import settings
        from django.utils import timezone

        uf = _get_uploaded_file(file_id)
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])

        uf.file.open("rb")
        wb_in = openpyxl.load_workbook(uf.file, data_only=True)
        ws    = wb_in.active
        all_rows = list(ws.iter_rows(values_only=True))

        if not all_rows:
            raise ValueError("Spreadsheet is empty.")

        original_headers = [str(h or "").strip() for h in all_rows[0]]
        data_rows = []
        for row in all_rows[1:]:
            if any(cell is not None and str(cell).strip() for cell in row):
                data_rows.append([str(c).strip() if c is not None else "" for c in row])

        wb_out = openpyxl.Workbook()
        ws_out = wb_out.active
        ws_out.title = "ACON Cleaned"
        ws_out.append(original_headers)
        for row in data_rows:
            ws_out.append(row)

        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(uf.original_filename).stem
        out_path = out_dir / f"{stem}_cleaned.xlsx"
        wb_out.save(out_path)

        uf.parse_status = "parsed"
        uf.parsed_at    = timezone.now()
        uf.save(update_fields=["parse_status", "parsed_at"])

        return {
            "ok":              True,
            "file_id":         file_id,
            "record_count":    len(data_rows),
            "headers":         original_headers,
            "rows":            data_rows[:5],
            "output_filename": str(out_path),
            "summary": (
                f"Cleaned {len(data_rows)} ACON records from '{uf.original_filename}'. "
                f"{len(original_headers)} columns retained. "
                f"Output saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("clean_acon_export(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 5. reconcile_ura_vs_acon
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_ura_vs_acon(ura_file_id: int, acon_file_id: int) -> dict:
    """
    Cross-reference URA fiscal receipt records against ACON export records.

    Matches on CU Invoice Number (URA) vs. the invoice/item number column (ACON).
    Returns variance rows where amounts differ, plus unmatched records.
    """
    try:
        import openpyxl
        from django.conf import settings

        ura_uf  = _get_uploaded_file(ura_file_id)
        acon_uf = _get_uploaded_file(acon_file_id)

        # ── Load URA data ─────────────────────────────────────────────
        ura_uf.file.open("rb")
        raw = ura_uf.file.read().decode("utf-8", errors="replace")
        BLOCK_RE = re.compile(
            r'^(?P<entry_type>FISCAL RECEIPT|CREDIT NOTE)\s*\r?\n'
            r'CU INVOICE NUMBER:\s*(?P<cu_no>\S+)\s*\r?\n'
            r'(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s*\r?\n'
            r'TOTAL:\s+(?P<total>[\d\s,]+)\r?\n'
            r'TAXES:\s+(?P<taxes>[\d\s,]+)',
            re.MULTILINE | re.IGNORECASE,
        )
        ura_records = {
            m.group("cu_no").strip(): {
                "total":  _norm_number(m.group("total")),
                "taxes":  _norm_number(m.group("taxes")),
                "date":   m.group("date"),
                "type":   m.group("entry_type").title(),
            }
            for m in BLOCK_RE.finditer(raw)
        }

        # ── Load ACON data ────────────────────────────────────────────
        acon_uf.file.open("rb")
        wb_acon = openpyxl.load_workbook(acon_uf.file, data_only=True)
        ws_acon = wb_acon.active
        acon_rows = list(ws_acon.iter_rows(values_only=True))
        acon_headers = [str(h or "").lower().strip() for h in acon_rows[0]]

        def _col(name: str) -> int | None:
            for i, h in enumerate(acon_headers):
                if name in h:
                    return i
            return None

        inv_col = _col("number") or _col("invoice") or _col("item")
        amt_col = _col("lc amount") or _col("amount")

        acon_map = {}
        if inv_col is not None and amt_col is not None:
            for row in acon_rows[1:]:
                key = str(row[inv_col] or "").strip()
                try:
                    amt = float(str(row[amt_col] or "0").replace(",", ""))
                except ValueError:
                    amt = 0.0
                if key:
                    acon_map[key] = amt

        # ── Compare ───────────────────────────────────────────────────
        matched = unmatched_ura = variance = 0
        variance_rows = []

        for cu_no, ura in ura_records.items():
            acon_amt = acon_map.get(cu_no)
            if acon_amt is None:
                unmatched_ura += 1
                variance_rows.append({
                    "cu_invoice_number": cu_no,
                    "ura_total":         ura["total"],
                    "acon_amount":       None,
                    "difference":        ura["total"],
                    "status":            "UNMATCHED — not in ACON",
                    "date":              ura["date"],
                })
            else:
                diff = round(abs(ura["total"] - acon_amt), 2)
                if diff > 0.01:
                    variance += 1
                    variance_rows.append({
                        "cu_invoice_number": cu_no,
                        "ura_total":         ura["total"],
                        "acon_amount":       acon_amt,
                        "difference":        diff,
                        "status":            "VARIANCE",
                        "date":              ura["date"],
                    })
                else:
                    matched += 1

        # ── Write variance xlsx ───────────────────────────────────────
        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "ura_acon_variance.xlsx"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Variance"
        ws.append(["CU Invoice Number", "URA Total", "ACON Amount", "Difference", "Status", "Date"])
        for vr in variance_rows:
            ws.append([
                vr["cu_invoice_number"], vr["ura_total"],
                vr["acon_amount"], vr["difference"],
                vr["status"], vr["date"],
            ])
        wb.save(out_path)

        total = len(ura_records)
        return {
            "ok":               True,
            "total_ura":        total,
            "matched":          matched,
            "unmatched_ura":    unmatched_ura,
            "variance_count":   variance,
            "variance_rows":    variance_rows[:10],   # preview
            "output_filename":  str(out_path),
            "summary": (
                f"Reconciliation complete. {total} URA records vs ACON export. "
                f"Matched: {matched} | Unmatched: {unmatched_ura} | Variances: {variance}. "
                f"Variance report saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("reconcile_ura_vs_acon: %s", exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 6. flag_anomalies
# ─────────────────────────────────────────────────────────────────────────────

def flag_anomalies(file_id: int) -> dict:
    """
    Scan extracted receipt/invoice data for anomalies:
    - Duplicate CU invoice numbers
    - Unusually large totals (> mean + 3 * std)
    - Round number totals (potential estimates)
    - Zero-value entries
    - Missing tax on non-zero totals

    Works on already-extracted .txt files.
    """
    try:
        uf = _get_uploaded_file(file_id)
        raw = ""

        if uf.extension == "txt":
            uf.file.open("rb")
            raw = uf.file.read().decode("utf-8", errors="replace")
        elif uf.extracted_text:
            raw = uf.extracted_text
        else:
            return {"ok": False, "error": "No text content available for this file."}

        BLOCK_RE = re.compile(
            r'^(?P<entry_type>FISCAL RECEIPT|CREDIT NOTE)\s*\r?\n'
            r'CU INVOICE NUMBER:\s*(?P<cu_no>\S+)\s*\r?\n'
            r'(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s*\r?\n'
            r'TOTAL:\s+(?P<total>[\d\s,]+)\r?\n'
            r'TAXES:\s+(?P<taxes>[\d\s,]+)',
            re.MULTILINE | re.IGNORECASE,
        )

        records = [
            {
                "cu_no":  m.group("cu_no").strip(),
                "total":  _norm_number(m.group("total")),
                "taxes":  _norm_number(m.group("taxes")),
                "date":   m.group("date"),
                "type":   m.group("entry_type").title(),
            }
            for m in BLOCK_RE.finditer(raw)
        ]

        if not records:
            return {"ok": False, "error": "No receipt blocks found in file."}

        totals = [r["total"] for r in records]
        mean   = sum(totals) / len(totals)
        std    = (sum((t - mean) ** 2 for t in totals) / len(totals)) ** 0.5
        threshold = mean + 3 * std

        seen_cu   = {}
        anomalies = []

        for r in records:
            # Duplicate CU number
            if r["cu_no"] in seen_cu:
                anomalies.append({
                    "cu_invoice_number": r["cu_no"],
                    "anomaly_type": "duplicate_cu_number",
                    "severity": "critical",
                    "detail": f"CU number also appears at record #{seen_cu[r['cu_no']]}.",
                    "value": r["total"],
                    "date": r["date"],
                })
            else:
                seen_cu[r["cu_no"]] = records.index(r) + 1

            # Unusually large total
            if r["total"] > threshold:
                anomalies.append({
                    "cu_invoice_number": r["cu_no"],
                    "anomaly_type": "unusually_large_total",
                    "severity": "warning",
                    "detail": f"Total {r['total']:,.2f} is more than 3 std deviations above mean ({mean:,.2f}).",
                    "value": r["total"],
                    "date": r["date"],
                })

            # Round number (possible estimate)
            if r["total"] > 0 and r["total"] % 1000 == 0:
                anomalies.append({
                    "cu_invoice_number": r["cu_no"],
                    "anomaly_type": "round_number_total",
                    "severity": "info",
                    "detail": f"Total {r['total']:,.0f} is a round number — may be an estimate.",
                    "value": r["total"],
                    "date": r["date"],
                })

            # Zero total
            if r["total"] == 0:
                anomalies.append({
                    "cu_invoice_number": r["cu_no"],
                    "anomaly_type": "zero_total",
                    "severity": "warning",
                    "detail": "Total is zero.",
                    "value": 0,
                    "date": r["date"],
                })

            # Non-zero total but zero tax (only flag fiscal receipts, not credit notes)
            if r["type"] == "Fiscal Receipt" and r["total"] > 0 and r["taxes"] == 0:
                anomalies.append({
                    "cu_invoice_number": r["cu_no"],
                    "anomaly_type": "zero_tax_on_receipt",
                    "severity": "info",
                    "detail": f"Fiscal receipt with total {r['total']:,.2f} has zero taxes.",
                    "value": r["total"],
                    "date": r["date"],
                })

        critical = sum(1 for a in anomalies if a["severity"] == "critical")
        warning  = sum(1 for a in anomalies if a["severity"] == "warning")
        info     = sum(1 for a in anomalies if a["severity"] == "info")

        return {
            "ok":            True,
            "file_id":       file_id,
            "records_scanned": len(records),
            "anomaly_count": len(anomalies),
            "critical":      critical,
            "warning":       warning,
            "info":          info,
            "anomalies":     anomalies[:20],   # preview
            "summary": (
                f"Scanned {len(records)} records. Found {len(anomalies)} anomalies: "
                f"{critical} critical, {warning} warnings, {info} info. "
                f"Review critical and warning items before filing."
            ),
        }

    except Exception as exc:
        logger.exception("flag_anomalies(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 7. generate_report
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(file_id: int, report_type: str = "ura_sales") -> dict:
    """
    Generate a formatted .xlsx report for a processed file.

    report_type options:
        ura_sales         — URA receipts with totals, tax, net, entry type
        safaricom_dept    — Safaricom lines grouped by department
        variance_summary  — cross-check variances
        acon_summary      — ACON records with LC/vatable breakdown
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from django.conf import settings
        from django.utils import timezone as tz

        uf = _get_uploaded_file(file_id)

        wb = openpyxl.Workbook()
        ws = wb.active

        HDR_FONT  = Font(bold=True, color="FFFFFF")
        HDR_FILL  = PatternFill("solid", fgColor="1F4E79")
        HDR_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)

        def _style_headers(worksheet, headers):
            worksheet.append(headers)
            for cell in worksheet[1]:
                cell.font, cell.fill, cell.alignment = HDR_FONT, HDR_FILL, HDR_ALIGN

        if report_type == "ura_sales":
            ws.title = "URA Sales Report"
            headers = [
                "CU Invoice Number", "Date", "Time",
                "Total (UGX)", "Taxes (UGX)", "Net Amount (UGX)", "Entry Type",
            ]
            _style_headers(ws, headers)

            uf.file.open("rb")
            raw = uf.file.read().decode("utf-8", errors="replace")
            BLOCK_RE = re.compile(
                r'^(?P<entry_type>FISCAL RECEIPT|CREDIT NOTE)\s*\r?\n'
                r'CU INVOICE NUMBER:\s*(?P<cu_no>\S+)\s*\r?\n'
                r'(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s*\r?\n'
                r'TOTAL:\s+(?P<total>[\d\s,]+)\r?\n'
                r'TAXES:\s+(?P<taxes>[\d\s,]+)',
                re.MULTILINE | re.IGNORECASE,
            )
            total_sum = taxes_sum = 0.0
            row_count = 0
            for m in BLOCK_RE.finditer(raw):
                total = _norm_number(m.group("total"))
                taxes = _norm_number(m.group("taxes"))
                net   = round(total - taxes, 2)
                ws.append([
                    m.group("cu_no").strip(),
                    m.group("date").strip(),
                    m.group("time").strip(),
                    total, taxes, net,
                    m.group("entry_type").strip().title(),
                ])
                total_sum += total
                taxes_sum += taxes
                row_count += 1

            # Totals row
            ws.append(["TOTAL", "", "", round(total_sum, 2), round(taxes_sum, 2),
                        round(total_sum - taxes_sum, 2), ""])
            for cell in ws[ws.max_row]:
                cell.font = Font(bold=True)

        else:
            ws.title = report_type.replace("_", " ").title()
            ws.append([f"Report type '{report_type}' — no data available for this file."])
            row_count = 0

        # Auto-width
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 50)

        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem     = Path(uf.original_filename).stem
        out_path = out_dir / f"{stem}_{report_type}_report.xlsx"
        wb.save(out_path)

        return {
            "ok":              True,
            "file_id":         file_id,
            "report_type":     report_type,
            "record_count":    row_count,
            "output_filename": str(out_path),
            "summary": (
                f"Generated '{report_type}' report for '{uf.original_filename}' "
                f"({row_count} data rows). Saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("generate_report(%s, %s): %s", file_id, report_type, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 8. summarise_batch
# ─────────────────────────────────────────────────────────────────────────────

def summarise_batch(batch_id: int) -> dict:
    """
    Return a structured summary of an UploadBatch:
    - file count and types
    - parse status breakdown
    - total records extracted so far
    - any error files
    """
    try:
        from uploads.models import UploadBatch, UploadedFile

        batch = UploadBatch.objects.get(pk=batch_id)
        files = UploadedFile.objects.filter(batch=batch)

        status_counts = {}
        type_counts   = {}
        errors        = []

        for f in files:
            status_counts[f.parse_status] = status_counts.get(f.parse_status, 0) + 1
            type_counts[f.detected_type or f.extension or "unknown"] = (
                type_counts.get(f.detected_type or f.extension or "unknown", 0) + 1
            )
            if f.parse_status == "parse_error":
                errors.append({"filename": f.original_filename, "error": f.parse_error})

        return {
            "ok":            True,
            "batch_id":      batch_id,
            "batch_label":   batch.label,
            "batch_status":  batch.status,
            "total_files":   files.count(),
            "status_counts": status_counts,
            "type_counts":   type_counts,
            "error_files":   errors,
            "summary": (
                f"Batch '{batch.label}' has {files.count()} file(s). "
                f"Status: {status_counts}. "
                + (f"{len(errors)} file(s) have errors." if errors else "No errors.")
            ),
        }

    except Exception as exc:
        logger.exception("summarise_batch(%s): %s", batch_id, exc)
        return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 9. extract_file_universal
# ─────────────────────────────────────────────────────────────────────────────

def extract_file_universal(file_id: int, context: str = "") -> dict:
    """
    Universal file extraction tool.
    
    Handles ANY file type (TXT, CSV, XLSX, PDF, JSON, DOCX, XML, HTML, etc.)
    and extracts structured data using Grok AI for intelligent parsing.
    
    Args:
        file_id: ID of the UploadedFile record
        context: Optional domain context for smarter parsing (e.g., "invoice", "financial report")
        
    Returns:
        {
            "ok": bool,
            "file_id": int,
            "filename": str,
            "file_type": str,
            "structured_data": dict,
            "summary": str,
            "error": str (if ok=False)
        }
    """
    try:
        from tools.universal_extractor import UniversalFileExtractor
        
        uf = _get_uploaded_file(file_id)
        uf.file.open('rb')
        
        result = UniversalFileExtractor.extract(
            uf.file,
            uf.original_filename,
            context=context
        )
        
        uf.file.close()
        
        if result["success"]:
            # Update the file record with extracted data
            uf.detected_type = result["file_type"]
            uf.parse_status = "parsed"
            uf.save(update_fields=["detected_type", "parse_status"])
            
            record_count = len(result.get("structured_data", {}).get("records", []))
            return {
                "ok": True,
                "file_id": file_id,
                "filename": result["filename"],
                "file_type": result["file_type"],
                "structured_data": result["structured_data"],
                "record_count": record_count,
                "summary": f"Successfully extracted {result['file_type']} file with {record_count} records",
            }
        else:
            uf.parse_status = "parse_error"
            uf.parse_error = result.get("error", "Unknown error")
            uf.save(update_fields=["parse_status", "parse_error"])
            
            return {
                "ok": False,
                "file_id": file_id,
                "filename": result["filename"],
                "error": result.get("error", "Unknown error"),
            }
    
    except Exception as exc:
        logger.exception(f"extract_file_universal failed for file_id={file_id}: {exc}")
        try:
            uf = _get_uploaded_file(file_id)
            uf.parse_status = "parse_error"
            uf.parse_error = str(exc)
            uf.save(update_fields=["parse_status", "parse_error"])
        except Exception:
            pass
        
        return {
            "ok": False,
            "file_id": file_id,
            "error": str(exc),
        }
