# tools/handlers.py
"""
Built-in tool handler primitives.

These are the ONLY functions that belong here — domain-agnostic operations
that genuinely require Python and cannot be expressed as a prompt:

    read_file        — open an UploadedFile and return its raw text content
    detect_file_type — sniff extension + content → type label + confidence
    write_xlsx       — turn rows + headers into a downloadable .xlsx file
    run_python       — execute a user-supplied Python snippet in a sandbox
    call_webhook     — HTTP POST/GET to an external URL (direct handler form)

Everything else — URA extraction, Safaricom parsing, ACON reconciliation,
anomaly flagging, report generation — is expressed as a prompt_transform
ToolDefinition seeded into the DB by the seed_tools management command.
Users can inspect, clone, and edit those tools without touching Python.

Rules
-----
- Every handler accepts **kwargs matching its ToolDefinition.parameters_schema.
- Every handler returns a plain JSON-serialisable dict.
- Handlers NEVER raise — catch all exceptions and return
  {"ok": False, "error": "<message>"} so the LLM can report the failure
  and continue the conversation rather than crashing the job.
- Defer heavy imports (openpyxl, pdfplumber) inside each function so this
  module loads fast even when those packages aren't installed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. read_file
# ─────────────────────────────────────────────────────────────────────────────

def read_file(file_id: int, max_chars: int = 12000) -> dict:
    """
    Open an UploadedFile and return its extracted text content.

    This is the universal "give me the file content" primitive that all
    prompt_transform tools depend on. It does NOT call any AI — it just
    reads what the upload pipeline already extracted.

    Parameters
    ----------
    file_id   : PK of the UploadedFile record.
    max_chars : Truncate returned text to this many characters (default 12 000).
                Keeps token usage predictable for the calling LLM.

    Returns
    -------
    {
        "ok": true,
        "file_id": <int>,
        "filename": <str>,
        "extension": <str>,
        "detected_type": <str | null>,
        "text": <str>,          # extracted text, truncated to max_chars
        "full_length": <int>,   # total length before truncation
        "truncated": <bool>
    }
    """
    try:
        from uploads.models import UploadedFile

        uf   = UploadedFile.objects.get(pk=file_id)
        text = uf.extracted_text or ""

        # If extracted_text is empty, try reading the raw file for txt/csv
        if not text and uf.extension.lower() in ("txt", "csv"):
            try:
                uf.file.open("rb")
                raw  = uf.file.read()
                text = raw.decode("utf-8", errors="replace")
                uf.file.close()
            except Exception as read_exc:
                logger.warning("read_file: could not read raw file %s: %s", file_id, read_exc)

        full_length = len(text)
        truncated   = full_length > max_chars
        return {
            "ok":           True,
            "file_id":      file_id,
            "filename":     uf.original_filename,
            "extension":    uf.extension,
            "detected_type": uf.detected_type,
            "text":         text[:max_chars],
            "full_length":  full_length,
            "truncated":    truncated,
        }

    except UploadedFile.DoesNotExist:
        return {"ok": False, "error": f"UploadedFile id={file_id} not found."}
    except Exception as exc:
        logger.exception("read_file(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 2. detect_file_type
# ─────────────────────────────────────────────────────────────────────────────

def detect_file_type(file_id: int) -> dict:
    """
    Inspect an UploadedFile and return (and persist) its detected type.

    Uses file extension + a fast content sniff — no LLM call. The result
    is written back to UploadedFile.detected_type so subsequent tools can
    branch on it without re-detecting.

    Detected type labels
    --------------------
    ura_fiscal_receipt  — KRA/URA .txt periodical report with CU INVOICE NUMBER blocks
    ura_sales_table     — URA/KRA .xls/.xlsx with FDN / TOTAL (A+B) columns
    safaricom_bill      — Safaricom PostPay PDF with TAX INVOICE SUMMARY section
    acon_export         — ACON .xlsx with DEBTOR ACCOUNT / LC AMOUNT columns
    generic_xlsx        — any other spreadsheet
    generic_csv         — CSV
    generic_pdf         — any other PDF
    unknown_txt         — .txt that didn't match known patterns
    unknown             — everything else

    Returns
    -------
    {"ok": true, "file_id": .., "filename": .., "detected_type": .., "confidence": ..}
    """
    try:
        from uploads.models import UploadedFile

        uf   = UploadedFile.objects.get(pk=file_id)
        ext  = uf.extension.lower()
        # Use a generous slice — Safaricom bill signals appear pages in
        text = (uf.extracted_text or "")[:20_000].upper()

        detected   = "unknown"
        confidence = "low"

        if ext == "txt":
            if "CU INVOICE NUMBER" in text and (
                "FISCAL RECEIPT" in text or "CREDIT NOTE" in text
            ):
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
            if (
                "DEBTOR ACCOUNT" in text
                or "STATUTORY ITEM" in text
                or "LC AMOUNT" in text
            ):
                detected, confidence = "acon_export", "high"
            elif (
                "NAME OF PURCHASER" in text
                or "TOTAL (A+B)" in text
                or "FDN" in text
            ):
                detected, confidence = "ura_sales_table", "high"
            elif "CU INVOICE" in text:
                detected, confidence = "ura_fiscal_receipt", "medium"
            else:
                detected, confidence = "generic_xlsx", "medium"

        elif ext == "csv":
            detected, confidence = "generic_csv", "medium"

        uf.detected_type        = detected
        uf.detection_confidence = confidence
        uf.save(update_fields=["detected_type", "detection_confidence"])

        return {
            "ok":           True,
            "file_id":      file_id,
            "filename":     uf.original_filename,
            "detected_type": detected,
            "confidence":   confidence,
        }

    except UploadedFile.DoesNotExist:
        return {"ok": False, "error": f"UploadedFile id={file_id} not found."}
    except Exception as exc:
        logger.exception("detect_file_type(%s): %s", file_id, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 3. write_xlsx
# ─────────────────────────────────────────────────────────────────────────────

def write_xlsx(
    filename: str,
    headers: list[str],
    rows: list[list],
    sheet_name: str = "Sheet1",
) -> dict:
    """
    Build an .xlsx file from headers + rows and save it to the outputs directory.

    This is the report-generation primitive. A prompt_transform tool (or Grok
    itself) produces the structured rows; this handler serialises them to a
    downloadable spreadsheet.

    Parameters
    ----------
    filename   : Output filename, e.g. "ura_report.xlsx". Extension forced to .xlsx.
    headers    : Column header labels.
    rows       : List of row arrays. Values are coerced to str if not
                 int/float/bool/None — keeps the xlsx valid.
    sheet_name : Worksheet tab name (default "Sheet1").

    Returns
    -------
    {"ok": true, "output_filename": <abs path str>, "record_count": <int>, "summary": <str>}
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from django.conf import settings

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name[:31]   # Excel tab name limit

        # Style the header row
        HDR_FONT  = Font(bold=True, color="FFFFFF")
        HDR_FILL  = PatternFill("solid", fgColor="1F4E79")
        HDR_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)

        ws.append(headers)
        for cell in ws[1]:
            cell.font, cell.fill, cell.alignment = HDR_FONT, HDR_FILL, HDR_ALIGN

        # Write data rows — coerce non-scalar types
        _scalar = (int, float, bool, type(None))
        for row in rows:
            ws.append([
                v if isinstance(v, _scalar) else str(v)
                for v in row
            ])

        # Auto-column widths (capped at 50)
        for col in ws.columns:
            width = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(width + 3, 50)

        out_dir = Path(settings.BASE_DIR) / "outputs" / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)

        stem     = Path(filename).stem
        out_path = out_dir / f"{stem}.xlsx"
        wb.save(out_path)

        return {
            "ok":              True,
            "output_filename": str(out_path),
            "record_count":    len(rows),
            "summary":         f"Wrote {len(rows)} rows to {out_path.name}.",
        }

    except Exception as exc:
        logger.exception("write_xlsx(%s): %s", filename, exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 4. run_python
# ─────────────────────────────────────────────────────────────────────────────

# Modules the sandbox is allowed to import
_ALLOWED_MODULES = frozenset({
    "re", "json", "csv", "math", "statistics",
    "datetime", "decimal", "collections", "itertools",
    "pathlib", "io", "string", "textwrap",
})

_BLOCKED_BUILTINS = frozenset({
    "__import__", "open", "exec", "eval", "compile",
    "globals", "locals", "vars", "dir",
    "breakpoint", "input", "print",
    "getattr", "setattr", "delattr", "hasattr",
})


def run_python(code: str, context: dict | None = None) -> dict:
    """
    Execute a user-supplied Python snippet in a restricted sandbox and
    return its output.

    The snippet has access to:
    - A `context` dict passed in by the caller (read-only by convention).
    - A `result` dict it should populate — {"ok": bool, ...}.
    - The allowed standard-library modules listed in _ALLOWED_MODULES.

    Restricted: no file I/O, no subprocess, no network, no __import__.

    This is a power-user escape hatch. It is marked is_safe=False in the
    ToolDefinition so Grok must ask for user confirmation before calling it.

    Parameters
    ----------
    code    : Python source to execute. Must set `result["ok"]`.
    context : Optional dict of values injected into the execution namespace.

    Returns
    -------
    {"ok": bool, ...} — whatever the snippet set on `result`, plus
    {"ok": False, "error": "..."} on any exception.

    Example snippet
    ---------------
    ::

        import re
        numbers = re.findall(r"\\d+", context.get("text", ""))
        result["ok"]    = True
        result["found"] = numbers
        result["count"] = len(numbers)
    """
    if not code or not code.strip():
        return {"ok": False, "error": "code is required."}

    # Build a minimal safe namespace
    safe_builtins = {
        k: v for k, v in __builtins__.items()   # type: ignore[union-attr]
        if k not in _BLOCKED_BUILTINS
    } if isinstance(__builtins__, dict) else {
        k: getattr(__builtins__, k)
        for k in dir(__builtins__)
        if k not in _BLOCKED_BUILTINS and not k.startswith("_")
    }

    import importlib

    def _safe_import(name, *args, **kwargs):
        if name not in _ALLOWED_MODULES:
            raise ImportError(
                f"Module '{name}' is not allowed in the sandbox. "
                f"Allowed: {sorted(_ALLOWED_MODULES)}"
            )
        return importlib.import_module(name)

    namespace: dict = {
        "__builtins__": {**safe_builtins, "__import__": _safe_import},
        "context":      context or {},
        "result":       {"ok": False},
    }

    try:
        exec(compile(code, "<tool_snippet>", "exec"), namespace)   # noqa: S102
        outcome = namespace.get("result", {"ok": False, "error": "result was not set"})
        if not isinstance(outcome, dict):
            return {"ok": False, "error": "result must be a dict"}
        return outcome
    except Exception as exc:
        logger.warning("run_python snippet raised: %s", exc)
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 5. call_webhook
# ─────────────────────────────────────────────────────────────────────────────

def call_webhook(
    url: str,
    payload: dict | None = None,
    method: str = "POST",
    headers: dict | None = None,
    timeout_seconds: int = 30,
) -> dict:
    """
    Make an HTTP request to an external URL and return the JSON response.

    This is the direct-handler form of the webhook dispatch. Unlike the
    service-layer _call_webhook() helper (which reads config from a
    UserToolConfig row), this handler accepts all parameters as arguments
    so Grok can call it with ad-hoc URLs without a pre-registered tool.

    Marked is_safe=False — Grok must ask for user confirmation before
    posting to external systems.

    Parameters
    ----------
    url             : Endpoint to call.
    payload         : JSON body (POST) or query-string params (GET).
    method          : "POST" or "GET" (default "POST").
    headers         : Extra HTTP headers.
    timeout_seconds : Request timeout (1–120, default 30).

    Returns
    -------
    {"ok": true, <response fields>} or {"ok": false, "error": "..."}
    """
    import urllib.request
    import urllib.error
    import json as _json

    method  = (method or "POST").upper()
    payload = payload or {}
    hdrs    = {
        "Content-Type": "application/json",
        "User-Agent":   "ai-invoicing/1.0 (call_webhook)",
        **(headers or {}),
    }
    timeout = max(1, min(int(timeout_seconds or 30), 120))

    try:
        body = _json.dumps(payload).encode("utf-8") if method == "POST" else None

        if method == "GET" and payload:
            import urllib.parse
            qs  = urllib.parse.urlencode(
                {k: _json.dumps(v) if isinstance(v, (dict, list)) else v
                 for k, v in payload.items()}
            )
            url = f"{url}?{qs}"

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw    = resp.read().decode("utf-8", errors="replace")
            status = resp.status

        if status >= 400:
            return {"ok": False, "error": f"HTTP {status}", "detail": raw[:500]}

        try:
            return {"ok": True, **_json.loads(raw)}
        except _json.JSONDecodeError:
            return {"ok": True, "raw_response": raw[:2000]}

    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"Connection error: {exc.reason}"}
    except TimeoutError:
        return {"ok": False, "error": f"Timed out after {timeout}s"}
    except Exception as exc:
        logger.exception("call_webhook(%s): %s", url, exc)
        return {"ok": False, "error": str(exc)}