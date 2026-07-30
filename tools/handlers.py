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


def _norm_id(value) -> str:
    """Normalise a fiscal id (FDN / CU / statutory no) to a clean string,
    stripping float artefacts like a trailing '.0' from numeric cells."""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _find_col(headers, *keywords) -> int:
    """Index of the first header containing any keyword (case-insensitive), else -1."""
    low = [str(h or "").lower() for h in headers]
    for kw in keywords:
        for i, h in enumerate(low):
            if kw in h:
                return i
    return -1


def _read_tabular(uf, prefer=()) -> tuple[list, list]:
    """
    Load an .xls/.xlsx UploadedFile into (headers, data_rows).

    Picks the sheet whose header row best matches the `prefer` keywords (so we
    grab the real data sheet, not a 'Criteria' tab), and detects the header row
    as the first row with >= 4 non-empty cells.
    """
    ext = uf.extension.lower()
    sheets = []  # (name, rows)
    uf.file.open("rb")
    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(uf.file, data_only=True, read_only=True)
        for ws in wb.worksheets:
            sheets.append((ws.title, [list(r) for r in ws.iter_rows(values_only=True)]))
        wb.close()
    elif ext == "xls":
        import xlrd
        book = xlrd.open_workbook(file_contents=uf.file.read())
        for sh in book.sheets():
            sheets.append((sh.name, [sh.row_values(r) for r in range(sh.nrows)]))
    else:
        raise ValueError(f"_read_tabular: unsupported extension '{ext}'")

    def header_idx(rows):
        for i, r in enumerate(rows[:10]):
            if sum(1 for c in r if c not in (None, "")) >= 4:
                return i
        return 0

    best, best_score = None, -1
    for _name, rows in sheets:
        if not rows:
            continue
        hidx = header_idx(rows)
        hdr = [str(c).strip() if c is not None else "" for c in rows[hidx]]
        low = " | ".join(hdr).lower()
        kw_hits = sum(1 for kw in prefer if kw in low) if prefer else 0
        score = kw_hits * 1_000_000 + len(rows)  # keyword match dominates, size breaks ties
        if score > best_score:
            best_score, best = score, (hdr, rows, hidx)
    if not best:
        return [], []
    hdr, rows, hidx = best
    data = [[("" if c is None else c) for c in r]
            for r in rows[hidx + 1:] if any(c not in (None, "") for c in r)]
    return hdr, data


def _parse_fiscal_side(uf) -> list[dict]:
    """
    Parse a URA/KRA fiscal *sales* file into normalised rows:
        {fiscal_no, name, date, total, vat}

    Handles BOTH the KRA `.txt` periodical report (CU INVOICE NUMBER blocks) and
    the URA/KRA `.xls`/`.xlsx` sales tables (FDN / TOTAL (A+B) columns).
    """
    ext = uf.extension.lower()
    out = []
    if ext == "txt":
        uf.file.open("rb")
        raw = uf.file.read().decode("utf-8", errors="replace")
        block = re.compile(
            r'^(?P<t>FISCAL RECEIPT|CREDIT NOTE)\s*\r?\n'
            r'CU INVOICE NUMBER:\s*(?P<cu>\S+)\s*\r?\n'
            r'(?P<d>\d{2}-\d{2}-\d{4})\s+\d{2}:\d{2}:\d{2}\s*\r?\n'
            r'TOTAL:\s+(?P<tot>[\d\s,]+)\r?\n'
            r'TAXES:\s+(?P<tax>[\d\s,]+)',
            re.MULTILINE | re.IGNORECASE,
        )
        for m in block.finditer(raw):
            out.append({
                "fiscal_no": _norm_id(m.group("cu")),
                "name": "", "date": m.group("d"),
                "total": _norm_number(m.group("tot")),
                "vat": _norm_number(m.group("tax")),
            })
        return out

    hdr, rows = _read_tabular(uf, prefer=("fdn", "name of purchaser", "cu invoice", "total (a+b)"))
    c_no  = _find_col(hdr, "fdn", "cu invoice", "statutory", "invoice number")
    c_nm  = _find_col(hdr, "name of purchaser", "name")
    c_dt  = _find_col(hdr, "invoice date", "date")
    c_tot = _find_col(hdr, "total (a+b)", "total", "billed amount", "(ugx)(a)")
    c_vat = _find_col(hdr, "vat charged", "vat")
    for r in rows:
        if not (0 <= c_no < len(r)):
            continue
        fno = _norm_id(r[c_no])
        if not fno or fno.lower() in ("none", ""):
            continue
        out.append({
            "fiscal_no": fno,
            "name":  str(r[c_nm]).strip() if 0 <= c_nm < len(r) else "",
            "date":  str(r[c_dt]).strip() if 0 <= c_dt < len(r) else "",
            "total": _saf_num(r[c_tot]) if 0 <= c_tot < len(r) else None,
            "vat":   _saf_num(r[c_vat]) if 0 <= c_vat < len(r) else None,
        })
    return out


def _parse_acon_side(uf) -> list[dict]:
    """
    Parse an ACON export into normalised rows:
        {fiscal_no, item_number, name, amount}

    The fiscal join key is ACON's statutory column — 'Statutory Item No(For
    Download VAT)' (Kenya CU) or 'FDN' (Uganda) — NOT the internal Item Number.
    """
    hdr, rows = _read_tabular(uf, prefer=("debtor account", "statutory item", "fdn", "item number"))
    c_no   = _find_col(hdr, "statutory item", "fdn")
    c_item = _find_col(hdr, "item number")
    c_nm   = _find_col(hdr, "full name", "name")
    c_amt  = _find_col(hdr, "lc amount", "amount")
    out = []
    for r in rows:
        if not (0 <= c_no < len(r)):
            continue
        fno = _norm_id(r[c_no])
        if not fno or fno.lower() in ("none", ""):
            continue
        out.append({
            "fiscal_no":   fno,
            "item_number": _norm_id(r[c_item]) if 0 <= c_item < len(r) else "",
            "name":        str(r[c_nm]).strip() if 0 <= c_nm < len(r) else "",
            "amount":      _saf_num(r[c_amt]) if 0 <= c_amt < len(r) else None,
        })
    return out


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
        # Search a generous slice — key signals (e.g. "BILLED AMOUNT" in a
        # Safaricom bill) can appear several pages in, not just at the top.
        text = (uf.extracted_text or "")[:20000].upper()

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
            if "SAFARICOM" in text or "POSTPAY" in text or "BILLED AMOUNT" in text:
                detected, confidence = "safaricom_bill", "high"
            else:
                detected, confidence = "generic_pdf", "low"

        elif ext in ("xlsx", "xls"):
            if "DEBTOR ACCOUNT" in text or "STATUTORY ITEM" in text or "LC AMOUNT" in text:
                detected, confidence = "acon_export", "high"
            elif "NAME OF PURCHASER" in text or "TOTAL (A+B)" in text or "FDN" in text:
                detected, confidence = "ura_sales_table", "high"
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
    Parse a URA/KRA fiscal *sales* file and write a normalised .xlsx.

    Handles two input shapes automatically:
      • KRA `.txt` periodical report — FISCAL RECEIPT / CREDIT NOTE blocks with
        a CU INVOICE NUMBER, total and taxes.
      • URA/KRA `.xls`/`.xlsx` sales table — columns such as FDN, Name of
        Purchaser, Invoice Date and TOTAL (A+B).

    Returns {ok, record_count, headers, rows (preview), output_filename, summary}.
    """
    try:
        import openpyxl
        from django.conf import settings
        from django.utils import timezone

        uf = _get_uploaded_file(file_id)
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])

        # ── Tabular URA/KRA sales export (.xls/.xlsx) ─────────────────────────
        if uf.extension.lower() in ("xls", "xlsx"):
            recs = _parse_fiscal_side(uf)
            headers = ["Fiscal No (FDN/CU)", "Name of Purchaser", "Invoice Date", "Total", "VAT"]
            rows = [[r["fiscal_no"], r["name"], r["date"], r["total"], r["vat"]] for r in recs]

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "URA Sales"
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

            total_amt = sum(r["total"] or 0 for r in recs)
            return {
                "ok":              True,
                "file_id":         file_id,
                "record_count":    len(rows),
                "headers":         headers,
                "rows":            rows[:5],
                "output_filename": str(out_path),
                "summary": (
                    f"Extracted {len(rows)} URA sales records from "
                    f"'{uf.original_filename}'. Total amount {total_amt:,.2f}. "
                    f"Output saved to {out_path.name}."
                ),
            }

        # ── KRA `.txt` periodical report ──────────────────────────────────────
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

# Patterns for the per-subscriber TAX INVOICE pages and the summary table.
_SAF_RE_INV   = re.compile(r"Invoice Number\s+([A-Z0-9\-]+)", re.I)
_SAF_RE_CU    = re.compile(r"CU\s*INVOICE\s*NO[:\s]+(\d+)", re.I)
_SAF_RE_PHONE = re.compile(r"^\d{9}$")
_SAF_RE_INVNO = re.compile(r"^[A-Z]\d?-?\d{6,}$")


def _saf_num(raw) -> float | None:
    """Parse a Safaricom amount like '9,429.13' → 9429.13; '' → None."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _saf_department(name: str) -> str:
    """Best-effort: strip the Kuehne+Nagel company prefix to leave the unit/user."""
    d = re.sub(r"^\s*kuehne\s*\+?\s*nagel(\s+ltd)?\b[\s\-]*", "", name, flags=re.I)
    d = re.sub(r"^\s*kuehne\s+and\s+nagel(\s+ltd)?\b[\s\-]*", "", d, flags=re.I)
    return d.strip() or name


def extract_safaricom_bill(file_id: int) -> dict:
    """
    Extract a per-line telephone billing report from a Safaricom PostPay bill PDF.

    The bill has two relevant sections:
      • TAX INVOICE SUMMARY (a few pages): Name/department, phone (Reference NO.),
        invoice number, Net/VAT/Excise/Billed Amount — one row per subscriber.
      • One TAX INVOICE per subscriber (each spanning several pages) whose first
        page carries the fiscal "CU INVOICE NO" plus the invoice number.

    This joins the two on invoice number so every subscriber row gets its CU
    number, then writes an .xlsx billing report mapping
    telephone user → department → phone → invoice → CU number → amounts.
    """
    try:
        import pdfplumber
        import openpyxl
        from django.conf import settings
        from django.utils import timezone

        uf = _get_uploaded_file(file_id)
        uf.parse_status = "parsing"
        uf.save(update_fields=["parse_status"])

        detail_cu: dict[str, str] = {}   # invoice_no -> CU invoice number
        summary: dict[str, dict]  = {}   # invoice_no -> subscriber row (deduped)

        uf.file.open("rb")
        with pdfplumber.open(uf.file) as pdf:
            in_summary = False
            for page in pdf.pages:
                text = page.extract_text() or ""
                upper = text.upper()

                if "TAX INVOICE SUMMARY" in upper:
                    in_summary = True

                # A per-subscriber document page: end of the summary section,
                # and the place where the CU invoice number lives.
                if "SUBSCRIBER NUMBER" in upper:
                    in_summary = False
                    m_inv, m_cu = _SAF_RE_INV.search(text), _SAF_RE_CU.search(text)
                    if m_inv and m_cu:
                        detail_cu.setdefault(m_inv.group(1).strip(), m_cu.group(1).strip())
                    continue

                if in_summary:
                    for table in (page.extract_tables() or []):
                        for row in table:
                            cells = [str(c or "").replace("\n", " ").strip() for c in row]
                            if len(cells) < 7:
                                continue
                            phone = cells[1].replace(" ", "")
                            inv   = cells[2].replace(" ", "")
                            if _SAF_RE_PHONE.match(phone) and _SAF_RE_INVNO.match(inv):
                                summary[inv] = {
                                    "name": cells[0], "phone": phone, "invoice_no": inv,
                                    "net": cells[3], "vat": cells[4],
                                    "excise": cells[5], "billed": cells[6],
                                }

        headers = [
            "Telephone User", "Department", "Phone Number", "Invoice Number",
            "CU Invoice Number", "Net Amount", "VAT", "Excise", "Billed Amount",
        ]
        rows = []
        for inv, s in summary.items():
            rows.append([
                s["name"],
                _saf_department(s["name"]),
                s["phone"],
                inv,
                detail_cu.get(inv, ""),
                _saf_num(s["net"]),
                _saf_num(s["vat"]),
                _saf_num(s["excise"]),
                _saf_num(s["billed"]),
            ])

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Safaricom Billing"
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

        total_billed   = sum(r[8] or 0 for r in rows)
        without_cu      = sum(1 for r in rows if not r[4])
        return {
            "ok":              True,
            "file_id":         file_id,
            "record_count":    len(rows),
            "headers":         headers,
            "rows":            rows[:5],
            "output_filename": str(out_path),
            "total_billed":    round(total_billed, 2),
            "missing_cu_count": without_cu,
            "summary": (
                f"Extracted {len(rows)} telephone subscribers from Safaricom bill "
                f"'{uf.original_filename}'. Total billed Ksh {total_billed:,.2f}. "
                f"{len(rows) - without_cu}/{len(rows)} matched a CU invoice number. "
                f"Output saved to {out_path.name}."
            ),
        }

    except Exception as exc:
        logger.exception("extract_safaricom_bill(%s): %s", file_id, exc)
        try:
            uf = _get_uploaded_file(file_id)
            uf.parse_status = "parse_error"
            uf.parse_error  = str(exc)
            uf.save(update_fields=["parse_status", "parse_error"])
        except Exception:
            pass
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
    Reconcile a URA/KRA fiscal *sales* file against an ACON export.

    The join key is the FISCAL number — the URA/KRA FDN or CU Invoice Number
    matched against ACON's statutory column ('Statutory Item No(For Download
    VAT)' for Kenya, or 'FDN' for Uganda) — NOT ACON's internal Item Number.

    Produces a reconciliation workpaper (.xlsx) with one row per fiscal number:
    matched rows (with both amounts and the variance), records present in the
    fiscal file but MISSING_IN_ACON, and records in ACON but MISSING_IN_URA.
    """
    try:
        import openpyxl
        from django.conf import settings

        ura_uf  = _get_uploaded_file(ura_file_id)
        acon_uf = _get_uploaded_file(acon_file_id)

        ura_rows  = _parse_fiscal_side(ura_uf)
        acon_rows = _parse_acon_side(acon_uf)

        # Index ACON by fiscal number (first occurrence wins).
        acon_by_no = {}
        for a in acon_rows:
            acon_by_no.setdefault(a["fiscal_no"], a)
        ura_nos = {u["fiscal_no"] for u in ura_rows}

        TOL = 1.0
        matched, variances, unmatched_ura = [], [], []
        for u in ura_rows:
            a = acon_by_no.get(u["fiscal_no"])
            if not a:
                unmatched_ura.append(u)
                continue
            ut, at = u.get("total"), a.get("amount")
            var = round(ut - at, 2) if (ut is not None and at is not None) else None
            row = {
                "fiscal_no":   u["fiscal_no"],
                "name":        u.get("name") or a.get("name"),
                "date":        u.get("date"),
                "ura_total":   ut,
                "acon_item":   a.get("item_number"),
                "acon_amount": at,
                "variance":    var,
            }
            matched.append(row)
            if var is not None and abs(var) > TOL:
                variances.append(row)

        unmatched_acon = [a for a in acon_rows if a["fiscal_no"] not in ura_nos]

        # ── Write the reconciliation workpaper ────────────────────────────────
        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "ura_acon_reconciliation.xlsx"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Reconciliation"
        ws.append(["Fiscal No (FDN/CU)", "Purchaser", "Date",
                   "URA Total", "ACON Item No", "ACON Amount", "Variance", "Status"])
        for m in matched:
            status = "VARIANCE" if (m["variance"] is not None and abs(m["variance"]) > TOL) else "MATCH"
            ws.append([m["fiscal_no"], m["name"], m["date"], m["ura_total"],
                       m["acon_item"], m["acon_amount"], m["variance"], status])
        for u in unmatched_ura:
            ws.append([u["fiscal_no"], u.get("name"), u.get("date"), u.get("total"),
                       "", "", "", "MISSING_IN_ACON"])
        for a in unmatched_acon:
            ws.append([a["fiscal_no"], a.get("name"), "", "",
                       a.get("item_number"), a.get("amount"), "", "MISSING_IN_URA"])
        wb.save(out_path)

        return {
            "ok":                   True,
            "ura_count":            len(ura_rows),
            "acon_count":           len(acon_rows),
            "matched_count":        len(matched),
            "variance_count":       len(variances),
            "missing_in_acon":      len(unmatched_ura),
            "missing_in_ura":       len(unmatched_acon),
            "output_filename":      str(out_path),
            "rows":                 matched[:5],
            "summary": (
                f"Reconciled {len(ura_rows)} URA/KRA records against "
                f"{len(acon_rows)} ACON records (matched on fiscal number). "
                f"Matched {len(matched)}, of which {len(variances)} have amount "
                f"variances. Missing in ACON: {len(unmatched_ura)}; "
                f"missing in URA: {len(unmatched_acon)}. "
                f"Workpaper saved to {out_path.name}."
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
                "Fiscal No (FDN/CU)", "Name of Purchaser", "Date",
                "Total", "VAT", "Net Amount",
            ]
            _style_headers(ws, headers)

            # Use the shared parser so this works for BOTH the .txt CU periodical
            # report and the .xls/.xlsx URA sales tables (FDN columns).
            recs = _parse_fiscal_side(uf)
            total_sum = vat_sum = 0.0
            for r in recs:
                tot = r.get("total") or 0.0
                vat = r.get("vat") or 0.0
                ws.append([
                    r["fiscal_no"], r.get("name", ""), r.get("date", ""),
                    tot, vat, round(tot - vat, 2),
                ])
                total_sum += tot
                vat_sum   += vat
            row_count = len(recs)

            # Totals row
            ws.append(["TOTAL", "", "", round(total_sum, 2), round(vat_sum, 2),
                        round(total_sum - vat_sum, 2)])
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
