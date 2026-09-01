# tools/management/commands/seed_tools.py
"""
python manage.py seed_tools
python manage.py seed_tools --export /app/tools_export.json
python manage.py seed_tools --export-only /app/tools_export.json

Replaces both `register_tools` and `sync_tools_to_ui`.

What it does
------------
1. Creates / updates every built-in ToolDefinition (tool_type="builtin")
   pointing to an abstract primitive in tools/handlers.py.
2. Creates / updates every domain ToolDefinition (tool_type="prompt_transform")
   with its system_prompt stored in UserToolConfig — editable from Django admin
   without a code deploy.
3. Disables any ToolDefinition that is no longer in this manifest but was
   previously seeded by this command (created_by=None, tool_type != "webhook")
   so stale rows don't confuse Grok.
4. Optionally writes a JSON export of all enabled tools for the frontend
   (--export or --export-only flags).

Idempotent — safe to run on every deploy.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction


# ─────────────────────────────────────────────────────────────────────────────
# Tool manifest
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_TOOLS = [
    {
        "name":         "read_file",
        "display_name": "Read File",
        "description": (
            "Open an uploaded file and return its text content — for ANY file type "
            "(PDF, CSV, XLSX, DOCX, TXT, etc), not just PDFs. Use this before any "
            "prompt_transform tool that needs to analyse file content. "
            "For a large file, pass page_from (and page_to for PDFs) to read it in "
            "chunks — this works even while background extraction is still running. "
            "For PDFs, page_from/page_to select actual document pages. For every "
            "other file type, page_from selects one max_chars-sized chunk of the "
            "full text (page_to is ignored for non-PDFs — call again with "
            "page_from+1 to get the next chunk). "
            "Check 'truncated' and 'page_count' to see whether more content remains, and "
            "keep paging (incrementing page_from) until page_from reaches page_count "
            "before drawing conclusions from a large document — never assume a single "
            "call has the whole document just because it returned ok=true. "
            "Returns ok=false if the file has no readable text (e.g. a scanned image-only PDF) — "
            "in that case say so rather than inferring the contents."
        ),
        "category": "utility",
        "handler":  "tools.handlers.read_file",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id":   {
                    "type": "integer",
                    "description": "PK of the UploadedFile record.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "How much text this single call returns (default 40000). "
                        "Not a hard limit on the document — keep paging with "
                        "page_from to read all of it."
                    ),
                    "default": 40000,
                },
                "page_from": {
                    "type": "integer",
                    "description": (
                        "PDF: first page to read (1-indexed, inclusive). "
                        "Non-PDF: which max_chars-sized chunk to read (1-indexed). "
                        "Omit to read from the start."
                    ),
                },
                "page_to": {
                    "type": "integer",
                    "description": (
                        "Last PDF page to read (1-indexed, inclusive). PDFs only — "
                        "ignored for every other file type, which return one chunk "
                        "per call regardless. Omit to read to the end (PDFs)."
                    ),
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "detect_file_type",
        "display_name": "Detect File Type",
        "description": (
            "Inspect an uploaded file and determine its type using extension + content sniff. "
            "Always call this first when you don't know what kind of file you're dealing with. "
            "Persists the result to the file record so subsequent tools can branch on it. "
            "Returns: detected_type, confidence."
        ),
        "category": "utility",
        "handler":  "tools.handlers.detect_file_type",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile record.",
                },
            },
            "required": ["file_id"],
        },
    },
    # ── Domain builtins ───────────────────────────────────────────────────────
    # These parse the document in Python and write the .xlsx themselves. They
    # exist because the generic prompt_transform equivalents cannot reliably do
    # what they do: join a Safaricom bill's summary section to its per-subscriber
    # detail pages, and reconcile on the fiscal/statutory column specifically.
    # Prefer these whenever the file is one of these known shapes.
    {
        "name":         "extract_safaricom_bill",
        "display_name": "Extract Safaricom Bill",
        "description": (
            "Extract a per-line telephone billing report from a Safaricom PostPay bill PDF. "
            "Joins the TAX INVOICE SUMMARY rows to each subscriber's TAX INVOICE page on "
            "invoice number, so every line gets its CU Invoice Number, then writes an .xlsx "
            "mapping telephone user → department → phone → invoice → CU number → amounts. "
            "Use this for any Safaricom bill instead of a generic extractor — the CU number "
            "and the amounts live in different sections and must be joined. "
            "Returns: record_count, headers, rows (preview), output_filename."
        ),
        "category": "extraction",
        "handler":  "tools.handlers.extract_safaricom_bill",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile record (a Safaricom PostPay PDF).",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "reconcile_ura_vs_acon",
        "display_name": "Reconcile URA/KRA vs ACON",
        "description": (
            "Reconcile a URA/KRA fiscal sales file against an ACON export and write a "
            "variance workpaper (.xlsx). Joins on the FISCAL number — the URA/KRA FDN or "
            "CU Invoice Number matched against ACON's statutory column ('Statutory Item "
            "No(For Download VAT)' for Kenya, 'FDN' for Uganda) — NOT ACON's internal Item "
            "Number. Identifiers are normalised before matching, so values Excel stored as "
            "'12345.0' still match '12345'. Use this in preference to the generic "
            "reconcile_datasets whenever both sides are a fiscal file and an ACON export. "
            "Returns: matched_count, variance_count, missing_in_acon, missing_in_ura, "
            "output_filename."
        ),
        "category": "reconciliation",
        "handler":  "tools.handlers.reconcile_ura_vs_acon",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "ura_file_id": {
                    "type": "integer",
                    "description": "PK of the URA/KRA fiscal file (.txt periodical report or .xls/.xlsx sales table).",
                },
                "acon_file_id": {
                    "type": "integer",
                    "description": "PK of the ACON export (.xlsx).",
                },
            },
            "required": ["ura_file_id", "acon_file_id"],
        },
    },
    {
        "name":         "write_xlsx",
        "display_name": "Write XLSX",
        "description": (
            "Build a downloadable .xlsx spreadsheet from headers and rows. "
            "Call this after a prompt_transform tool has produced structured data "
            "and the user wants a formatted file. "
            "Returns: output_filename (absolute path), record_count."
        ),
        "category": "report",
        "handler":  "tools.handlers.write_xlsx",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "filename":   {
                    "type": "string",
                    "description": "Output filename, e.g. 'ura_report.xlsx'.",
                },
                "headers":    {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column header labels.",
                },
                "rows":       {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "List of row arrays.",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Worksheet tab name. Default 'Sheet1'.",
                    "default": "Sheet1",
                },
            },
            "required": ["filename", "headers", "rows"],
        },
    },
    {
        "name":         "export_file",
        "display_name": "Export File",
        "description": (
            "Convert an already-uploaded file's FULL content into a different "
            "downloadable format (xlsx, csv, json, or txt) by file_id — no "
            "re-upload needed, and not limited by your own context window. "
            "Use this instead of write_xlsx whenever the user asks to get a "
            "file back in another format (e.g. 'give me that as an Excel "
            "file', 'export this as CSV') — it reads the stored extraction "
            "directly and writes it byte-faithfully, rather than you having "
            "to re-type the data as headers/rows yourself. Works for a file "
            "from any earlier turn in this conversation, not just one "
            "attached to the current message — check the file listing in "
            "your context for the right file_id. "
            "Returns: output_filename (absolute path), format, chars."
        ),
        "category": "report",
        "handler":  "tools.handlers.export_file",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile record to export.",
                },
                "target_format": {
                    "type": "string",
                    "enum": ["xlsx", "csv", "json", "txt"],
                    "description": "Output format. Default 'xlsx'.",
                    "default": "xlsx",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "run_python",
        "display_name": "Run Python Snippet",
        "description": (
            "Execute a Python snippet in a restricted sandbox. "
            "Use for numeric calculations, regex extraction, or data reshaping "
            "that is too complex for a prompt. "
            "The snippet must set result['ok'] = True and populate any other keys. "
            "Allowed stdlib: re, json, csv, math, statistics, datetime, decimal, "
            "collections, itertools, pathlib, io, string, textwrap. "
            "No file I/O, no network, no subprocess. "
            "REQUIRES USER CONFIRMATION before execution."
        ),
        "category": "utility",
        "handler":  "tools.handlers.run_python",
        "is_safe":  False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "code":    {
                    "type": "string",
                    "description": "Python source code to execute.",
                },
                "context": {
                    "type": "object",
                    "description": "Values injected into the snippet namespace.",
                    "default": {},
                },
            },
            "required": ["code"],
        },
    },
    {
        "name":         "call_webhook",
        "display_name": "Call Webhook",
        "description": (
            "Make an HTTP POST or GET request to an external URL and return the JSON response. "
            "Use when the user wants to push data to or pull data from an external system. "
            "REQUIRES USER CONFIRMATION before execution."
        ),
        "category": "utility",
        "handler":  "tools.handlers.call_webhook",
        "is_safe":  False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url":             {
                    "type": "string",
                    "description": "Endpoint URL.",
                },
                "payload":         {
                    "type": "object",
                    "description": "Request body (POST) or query params (GET).",
                    "default": {},
                },
                "method":          {
                    "type": "string",
                    "description": "'POST' or 'GET'. Default 'POST'.",
                    "default": "POST",
                },
                "headers":         {
                    "type": "object",
                    "description": "Extra HTTP headers.",
                    "default": {},
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds (1–120). Default 30.",
                    "default": 30,
                },
            },
            "required": ["url"],
        },
    },
]


PROMPT_TRANSFORM_TOOLS = [
    {
        "name":         "extract_invoice_data",
        "display_name": "Extract Invoice Data",
        "description": (
            "Extract structured invoice data from any file — URA/KRA fiscal receipts, "
            "Safaricom bills, ACON exports, or generic invoices. "
            "Reads the file, identifies columns/blocks, and returns a JSON array of records. "
            "Accepts file_id and an optional context hint."
        ),
        "category": "extraction",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile to extract.",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional hint about what to extract, e.g. "
                        "'URA fiscal receipts', 'Safaricom billing', 'ACON export'."
                    ),
                    "default": "",
                },
            },
            "required": ["file_id"],
        },
        "system_prompt": """\
You are an expert financial document parser working for a finance team in East Africa.

You have been given the raw text content of an uploaded file.

Your job:
1. Identify the document type (URA fiscal receipt, KRA CU report, Safaricom PostPay bill,
   ACON export, generic invoice, or other).
2. Extract every record/line item into a JSON array.
3. Normalise all values:
   - Numbers: strip commas and spaces, return as numeric (not strings).
   - Dates: return as YYYY-MM-DD where possible.
   - IDs (invoice numbers, CU numbers, FDN): strip trailing ".0" float artefacts.
4. Return ONLY valid JSON — no prose, no markdown fences.

CRITICAL — fidelity to the source (this output feeds financial reconciliation,
where every value matters):
- Every record you output MUST correspond to a record that is literally present
  in the file content below. Never invent, estimate, infer, extrapolate, or
  fabricate a record that is not explicitly there.
- Never omit a record that IS present, and never merge two distinct records
  into one.
- If a single field's value is missing, illegible, or ambiguous, set that
  field to null — do not guess a value, and do not drop the whole record
  because one field is unclear.
- record_count MUST equal the exact number of records in the records array —
  never round, estimate, or approximate it.

Output format:
{
  "document_type": "<detected type>",
  "record_count": <int>,
  "records": [
    {
      "id": "<invoice/fiscal/CU number>",
      "date": "<YYYY-MM-DD or original>",
      "name": "<supplier/purchaser name if present>",
      "total": <number or null>,
      "tax": <number or null>,
      "net": <number or null>,
      "extra": {}
    }
  ],
  "summary": {
    "total_amount": <sum of totals>,
    "date_range": {"from": "...", "to": "..."}
  }
}

File content:
{file_text}

Context hint: {arguments}
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "document_type": {"type": "string"},
                "record_count":  {"type": "integer"},
                "records":       {"type": "array"},
                "summary":       {"type": "object"},
            },
        },
    },
    {
        "name":         "flag_anomalies",
        "display_name": "Flag Anomalies",
        "description": (
            "Scan a set of invoice or transaction records for anomalies: "
            "duplicate IDs, zero values, unusually large amounts, "
            "missing tax on taxable amounts, round-number estimates. "
            "Pass a file_id or a JSON array of already-extracted records."
        ),
        "category": "analysis",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile to scan.",
                },
                "records": {
                    "type": "array",
                    "description": (
                        "Optional: pass already-extracted records instead of a file. "
                        "Each item must have at least an 'id' and 'total' field."
                    ),
                    "default": [],
                },
            },
        },
        "system_prompt": """\
You are a financial auditor specialising in data quality checks for East African invoice data.

You have been given either:
(a) the raw text of a fiscal file, or
(b) a JSON array of already-extracted invoice records.

Scan every record and flag anomalies using these checks:
1. DUPLICATE_ID        — same invoice/CU/FDN number appears more than once
2. ZERO_TOTAL          — total is 0 or null on a non-credit-note entry
3. ZERO_TAX            — fiscal receipt with positive total but zero tax
4. STATISTICAL_OUTLIER — total more than 3 standard deviations above the mean
5. ROUND_NUMBER        — total is an exact multiple of 1000 (possible estimate)
6. MISSING_DATE        — date field is empty or unparseable
7. NEGATIVE_AMOUNT     — total or tax is negative on a non-credit entry

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "records_scanned": <int>,
  "anomaly_count": <int>,
  "critical": <int>,
  "warning": <int>,
  "info": <int>,
  "anomalies": [
    {
      "id": "<invoice/CU number>",
      "anomaly_type": "<DUPLICATE_ID | ZERO_TOTAL | ...>",
      "severity": "<critical | warning | info>",
      "detail": "<one sentence explanation>",
      "value": <the offending value or null>
    }
  ]
}

File content (if provided):
{file_text}

Records (if provided directly):
{arguments}
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "records_scanned": {"type": "integer"},
                "anomaly_count":   {"type": "integer"},
                "anomalies":       {"type": "array"},
            },
        },
    },
    {
        "name":         "reconcile_datasets",
        "display_name": "Reconcile Datasets",
        "description": (
            "Compare two sets of records on a shared key and produce a variance report. "
            "Works for URA vs ACON, any two invoice exports, or any two lists with a common ID. "
            "Returns matched rows, variances, and rows missing from either side. "
            "If both sides live in ONE workbook as separate tabs (e.g. a combined bank-ledger-"
            "and-M-Pesa-statement export), pass the SAME file id as both file_id_a and file_id_b "
            "and set sheet_a/sheet_b to the two tab names (check read_file or detect_file_type "
            "output for the exact sheet names first) — otherwise both sides resolve to the same "
            "flattened text and everything trivially matches itself."
        ),
        "category": "reconciliation",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id_a":   {"type": "integer", "description": "PK of the first file."},
                "file_id_b":   {"type": "integer", "description": "PK of the second file."},
                "sheet_a": {
                    "type": "string",
                    "description": (
                        "Worksheet name for side A, only needed when file_id_a and file_id_b "
                        "are the SAME workbook and the two sides are separate tabs within it."
                    ),
                    "default": "",
                },
                "sheet_b": {
                    "type": "string",
                    "description": "Worksheet name for side B — see sheet_a.",
                    "default": "",
                },
                "join_key":    {
                    "type": "string",
                    "description": "Field to join on. Default: inferred.",
                    "default": "",
                },
                "amount_key_a": {"type": "string", "description": "Amount field in file A. Default: inferred.", "default": ""},
                "amount_key_b": {"type": "string", "description": "Amount field in file B. Default: inferred.", "default": ""},
                "tolerance":   {
                    "type": "number",
                    "description": "Variance tolerance. Default 1.0.",
                    "default": 1.0,
                },
            },
            "required": ["file_id_a", "file_id_b"],
        },
        "system_prompt": """\
You are a financial reconciliation specialist.

You have been given the text content of TWO files. Reconcile them.

Steps:
1. Parse each file and extract its records. Identify the natural join key
   (invoice number, fiscal number, CU number, FDN, or whatever is common).
   If join_key is specified in arguments, use that.
2. Match records from file A to file B on the join key.
3. For matched pairs, compute variance (amount_A - amount_B).
   Tolerance: differences below tolerance are "MATCH", above are "VARIANCE".
4. List records in A with no match in B: MISSING_IN_B.
5. List records in B with no match in A: MISSING_IN_A.

CRITICAL — this comparison must be complete and faithful to the source data:
- Every single record present in File A and every single record present in
  File B must appear in the output "rows" array exactly once — either as one
  side of a MATCH/VARIANCE pair, or as a standalone MISSING_IN_B/MISSING_IN_A
  entry. Do not silently drop a record from the comparison for any reason.
- Never invent, estimate, or fabricate a record, on either side, that is not
  literally present in that file's content below.
- count_a and count_b MUST equal the exact number of records you found in
  each file — never round or approximate.
- If a field needed for matching or variance (join key, amount) is missing on
  a given record, still include that record with the missing field as null —
  do not drop it from the output.

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "join_key_used": "<field name>",
  "count_a": <int>,
  "count_b": <int>,
  "matched": <int>,
  "variance_count": <int>,
  "missing_in_b": <int>,
  "missing_in_a": <int>,
  "rows": [
    {
      "id": "<join key value>",
      "amount_a": <number or null>,
      "amount_b": <number or null>,
      "variance": <number or null>,
      "status": "MATCH | VARIANCE | MISSING_IN_B | MISSING_IN_A"
    }
  ],
  "summary": "<one paragraph plain English summary>"
}

File A content (file_id_a):
{file_a_text}

File B content (file_id_b):
{file_b_text}

Arguments (join_key, amount_key_a, amount_key_b, tolerance):
{arguments}

If either file's content above is empty, do NOT invent records — return
"count_a"/"count_b" of 0 for that side and say so in the summary.
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "join_key_used":  {"type": "string"},
                "count_a":        {"type": "integer"},
                "count_b":        {"type": "integer"},
                "matched":        {"type": "integer"},
                "variance_count": {"type": "integer"},
                "rows":           {"type": "array"},
                "summary":        {"type": "string"},
            },
        },
    },
    {
        "name":         "summarise_batch",
        "display_name": "Summarise Batch",
        "description": (
            "Produce a plain-English and structured summary of an upload batch: "
            "file count, types, parse status breakdown, errors, and high-level "
            "overview of the data inside. Accepts batch_id."
        ),
        "category": "analysis",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "batch_id": {
                    "type": "integer",
                    "description": "PK of the UploadBatch to summarise.",
                },
            },
            "required": ["batch_id"],
        },
        "system_prompt": """\
You are a data analyst summarising an upload batch for a finance team.

You have been given metadata and text content of files in a batch.

Your job:
1. Count files by type and parse status.
2. Identify files with errors and describe them briefly.
3. For parsed files, give a one-sentence summary of each
   (record count, date range, total amounts if visible).
4. Produce an overall batch summary.

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "batch_label": "<str>",
  "total_files": <int>,
  "by_type": {"<type>": <count>},
  "by_status": {"parsed": <n>, "parse_error": <n>},
  "error_files": [{"filename": "...", "error": "..."}],
  "file_summaries": [{"filename": "...", "summary": "..."}],
  "overall": {
    "total_records": <int or null>,
    "total_amount": <number or null>,
    "date_range": {"from": "...", "to": "..."}
  },
  "narrative": "<two to three sentence plain English summary>"
}

Batch file contents (one labelled block per file in the batch):
{batch_text}

Arguments:
{arguments}

If the batch content above is empty, do NOT invent records — report
"file_count": 0 and say the batch had no readable content.
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "batch_label": {"type": "string"},
                "total_files": {"type": "integer"},
                "by_type":     {"type": "object"},
                "by_status":   {"type": "object"},
                "narrative":   {"type": "string"},
            },
        },
    },
    {
        "name":         "clean_dataset",
        "display_name": "Clean Dataset",
        "description": (
            "Normalise and clean any tabular dataset: strip empty rows, "
            "standardise date formats, fix float artefacts on IDs, "
            "deduplicate, and return clean JSON records ready for "
            "downstream processing or write_xlsx."
        ),
        "category": "transformation",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "PK of the UploadedFile to clean.",
                },
                "operations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Cleaning operations to apply. Options: strip_empty, "
                        "deduplicate, normalise_dates, fix_ids, normalise_numbers. "
                        "Default: all."
                    ),
                    "default": [
                        "strip_empty", "deduplicate", "normalise_dates",
                        "fix_ids", "normalise_numbers",
                    ],
                },
            },
            "required": ["file_id"],
        },
        "system_prompt": """\
You are a data cleaning specialist. You have been given raw file content.

Apply the requested cleaning operations:
- strip_empty:       remove rows where all fields are blank
- deduplicate:       remove rows with identical ID fields
- normalise_dates:   convert all date strings to YYYY-MM-DD
- fix_ids:           strip trailing ".0" from numeric ID strings
- normalise_numbers: strip commas/spaces from numeric strings, convert to numbers

Operations requested: {arguments}

CRITICAL — fidelity to the source:
- A record may ONLY be removed or merged if an explicitly requested operation
  (strip_empty or deduplicate) justifies it. Never drop, invent, or fabricate
  a record for any other reason.
- Every field value that isn't touched by a requested normalisation operation
  must be carried through unchanged from the source — do not "improve",
  guess, or rewrite values beyond what was explicitly requested.
- original_count MUST equal the exact number of records found in the source
  content below, before any operation is applied.
- cleaned_count + removed_count MUST equal original_count.

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "original_count": <int>,
  "cleaned_count": <int>,
  "removed_count": <int>,
  "operations_applied": ["..."],
  "records": [{ <field>: <cleaned value> }],
  "notes": "<any data quality observations>"
}

File content:
{file_text}
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "original_count": {"type": "integer"},
                "cleaned_count":  {"type": "integer"},
                "records":        {"type": "array"},
                "notes":          {"type": "string"},
            },
        },
    },
]

# All names this command owns — used to disable stale seeded rows
ALL_SEEDED_NAMES = (
    {t["name"] for t in BUILTIN_TOOLS}
    | {t["name"] for t in PROMPT_TRANSFORM_TOOLS}
)


# ─────────────────────────────────────────────────────────────────────────────
# Command
# ─────────────────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Seed / update all built-in ToolDefinitions. "
        "Replaces both register_tools and sync_tools_to_ui."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--export",
            metavar="PATH",
            help=(
                "After seeding, write a JSON export of all enabled tools to PATH. "
                "Used by the frontend to load tool metadata without an API call."
            ),
        )
        parser.add_argument(
            "--export-only",
            metavar="PATH",
            help="Skip seeding; only export the current tool list to PATH.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from tools.models import ToolDefinition, UserToolConfig

        export_path  = options.get("export")
        export_only  = options.get("export_only")

        if not export_only:
            self._seed(ToolDefinition, UserToolConfig)

        if export_path or export_only:
            target = export_only or export_path
            self._export(ToolDefinition, target)

    # ── Seed ──────────────────────────────────────────────────────────────────

    def _seed(self, ToolDefinition, UserToolConfig):
        created = updated = disabled = 0

        # ── Builtin primitives ────────────────────────────────────────────────
        for spec in BUILTIN_TOOLS:
            _, was_created = ToolDefinition.objects.update_or_create(
                name=spec["name"],
                defaults={
                    "display_name":      spec["display_name"],
                    "description":       spec["description"],
                    "category":          spec["category"],
                    "tool_type":         "builtin",
                    "handler":           spec["handler"],
                    "parameters_schema": spec["parameters_schema"],
                    "is_safe":           spec.get("is_safe", True),
                    "enabled":           True,
                    "created_by":        None,
                },
            )
            label = "created" if was_created else "updated"
            self.stdout.write(f"  [{label}] builtin: {spec['name']}")
            if was_created:
                created += 1
            else:
                updated += 1

        # ── Prompt transform tools ────────────────────────────────────────────
        for spec in PROMPT_TRANSFORM_TOOLS:
            obj, was_created = ToolDefinition.objects.update_or_create(
                name=spec["name"],
                defaults={
                    "display_name":      spec["display_name"],
                    "description":       spec["description"],
                    "category":          spec["category"],
                    "tool_type":         "prompt_transform",
                    "handler":           "",
                    "parameters_schema": spec["parameters_schema"],
                    "is_safe":           spec.get("is_safe", True),
                    "enabled":           True,
                    "created_by":        None,
                },
            )
            UserToolConfig.objects.update_or_create(
                tool=obj,
                defaults={
                    "system_prompt": spec["system_prompt"],
                    "output_schema": spec.get("output_schema"),
                    "webhook_url":   "",
                },
            )
            label = "created" if was_created else "updated"
            self.stdout.write(f"  [{label}] prompt_transform: {spec['name']}")
            if was_created:
                created += 1
            else:
                updated += 1

        # ── Disable stale system tools no longer in the manifest ──────────────
        # Only touches rows where created_by=None and tool_type != "webhook"
        # (user-defined webhook tools are never auto-disabled).
        stale_qs = ToolDefinition.objects.filter(
            created_by=None,
            enabled=True,
        ).exclude(
            name__in=ALL_SEEDED_NAMES,
        ).exclude(
            tool_type="webhook",
        )
        for stale in stale_qs:
            stale.enabled = False
            stale.save(update_fields=["enabled"])
            self.stdout.write(
                self.style.WARNING(f"  [disabled] stale tool: {stale.name}")
            )
            disabled += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSeed complete — {created} created, {updated} updated, "
                f"{disabled} disabled."
            )
        )

    # ── Export ────────────────────────────────────────────────────────────────

    def _export(self, ToolDefinition, path: str):
        """
        Write a JSON file consumed by the frontend at build/startup time.
        Only uses real ToolDefinition model fields — no phantom attributes.
        """
        tools = (
            ToolDefinition.objects
            .filter(enabled=True)
            .select_related("user_config")
            .order_by("category", "name")
        )

        payload = []
        for tool in tools:
            entry = {
                "id":           tool.id,
                "name":         tool.name,
                "display_name": tool.display_name,
                "description":  tool.description,
                "category":     tool.category,
                "tool_type":    tool.tool_type,
                "is_safe":      tool.is_safe,
                "version":      tool.version,
                "grok_schema":  tool.to_grok_schema(),
                "parameters_schema": tool.parameters_schema,
                # Include prompt summary for prompt_transform so the UI can
                # show the user what the tool does without hitting the API.
                "has_prompt":   tool.tool_type == "prompt_transform",
                "is_custom":    tool.created_by_id is not None,
            }
            payload.append(entry)

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(payload)} tools → {out}"
            )
        )