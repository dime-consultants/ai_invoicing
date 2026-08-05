# tools/management/commands/seed_tools.py
"""
python manage.py seed_tools

Creates (or updates) every ToolDefinition the system ships with.

Builtin tools (tool_type="builtin") point to a Python handler in
tools/handlers.py — these are domain-agnostic primitives.

Domain tools (tool_type="prompt_transform") carry a system_prompt
that tells Grok how to perform the business task. They are editable
by admin users without touching Python code, and serve as the
reference implementation that users can clone to create their own
variants.

Running this command is idempotent — existing rows are updated in-place
so job history and ToolCall audit trails are preserved.
"""

from django.core.management.base import BaseCommand
from django.db import transaction


BUILTIN_TOOLS = [
    # ── Primitives ────────────────────────────────────────────────────────────
    {
        "name":         "read_file",
        "display_name": "Read File",
        "description": (
            "Open an uploaded file and return its extracted text content. "
            "Use this before any prompt_transform tool that needs to analyse file content. "
            "Returns: filename, extension, detected_type, text (up to max_chars), "
            "full_length, truncated."
        ),
        "category": "utility",
        "handler":  "tools.handlers.read_file",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id":   {"type": "integer", "description": "PK of the UploadedFile record."},
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate returned text to this many characters. Default 12000.",
                    "default": 12000,
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
                "file_id": {"type": "integer", "description": "PK of the UploadedFile record."},
            },
            "required": ["file_id"],
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
                "filename":   {"type": "string",  "description": "Output filename, e.g. 'ura_report.xlsx'."},
                "headers":    {"type": "array",   "items": {"type": "string"}, "description": "Column header labels."},
                "rows":       {"type": "array",   "items": {"type": "array"},  "description": "List of row arrays."},
                "sheet_name": {"type": "string",  "description": "Worksheet tab name. Default 'Sheet1'.", "default": "Sheet1"},
            },
            "required": ["filename", "headers", "rows"],
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
        "is_safe":  False,   # requires user confirmation
        "parameters_schema": {
            "type": "object",
            "properties": {
                "code":    {"type": "string", "description": "Python source code to execute."},
                "context": {"type": "object", "description": "Values injected into the snippet namespace.", "default": {}},
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
        "is_safe":  False,   # posts to external systems
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url":             {"type": "string",  "description": "Endpoint URL."},
                "payload":         {"type": "object",  "description": "Request body (POST) or query params (GET).", "default": {}},
                "method":          {"type": "string",  "description": "'POST' or 'GET'. Default 'POST'.", "default": "POST"},
                "headers":         {"type": "object",  "description": "Extra HTTP headers.", "default": {}},
                "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (1–120). Default 30.", "default": 30},
            },
            "required": ["url"],
        },
    },
]


PROMPT_TRANSFORM_TOOLS = [
    # ── Domain tools — editable by admins, cloneable by users ─────────────────
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
                "file_id": {"type": "integer", "description": "PK of the UploadedFile to extract."},
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
      "extra": {}   // any other fields found
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
            "duplicate IDs, zero values, unusually large amounts (statistical outlier), "
            "missing tax on taxable amounts, round-number estimates. "
            "Pass the file content or a JSON array of records."
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

Your job — scan every record and flag anomalies. Apply these checks:

1. DUPLICATE_ID       — same invoice/CU/FDN number appears more than once
2. ZERO_TOTAL         — total amount is 0 or null on a non-credit-note entry
3. ZERO_TAX           — fiscal receipt with a positive total but zero tax
4. STATISTICAL_OUTLIER — total is more than 3 standard deviations above the mean
5. ROUND_NUMBER       — total is an exact multiple of 1000 (possible estimate)
6. MISSING_DATE       — date field is empty or unparseable
7. NEGATIVE_AMOUNT    — total or tax is negative on a non-credit entry

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
            "Returns matched rows, variances, and rows missing from either side."
        ),
        "category": "reconciliation",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id_a": {
                    "type": "integer",
                    "description": "PK of the first file (e.g. URA/KRA fiscal export).",
                },
                "file_id_b": {
                    "type": "integer",
                    "description": "PK of the second file (e.g. ACON export).",
                },
                "join_key": {
                    "type": "string",
                    "description": (
                        "The field name to join on, e.g. 'fiscal_number', 'invoice_id'. "
                        "Default: the tool will infer the best join key."
                    ),
                    "default": "",
                },
                "amount_key_a": {
                    "type": "string",
                    "description": "Amount field in file A. Default: inferred.",
                    "default": "",
                },
                "amount_key_b": {
                    "type": "string",
                    "description": "Amount field in file B. Default: inferred.",
                    "default": "",
                },
                "tolerance": {
                    "type": "number",
                    "description": "Variance tolerance — differences below this are considered matched. Default 1.0.",
                    "default": 1.0,
                },
            },
            "required": ["file_id_a", "file_id_b"],
        },
        "system_prompt": """\
You are a financial reconciliation specialist.

You have been given the text content of TWO files. Your job is to reconcile them.

Steps:
1. Parse each file and extract its records. Identify the natural join key
   (invoice number, fiscal number, CU number, FDN, or whatever is common to both).
   If join_key is specified in the arguments, use that.
2. Match records from file A to file B on the join key.
3. For matched pairs, compute the variance (amount_A - amount_B).
   Tolerance for considering a pair "matched" is in the arguments (default 1.0).
4. List records in A that have no match in B (MISSING_IN_B).
5. List records in B that have no match in A (MISSING_IN_A).

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

File A content:
{file_text}

Arguments (file_id_b, join_key, tolerance, etc.):
{arguments}
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
            "how many files, what types, parse status breakdown, any errors, "
            "and a high-level overview of the data inside. "
            "Accepts batch_id."
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

You have been given metadata about an upload batch and the text content
of its files.

Your job:
1. Count files by type and parse status.
2. Identify any files with errors and describe the errors briefly.
3. For successfully parsed files, give a one-sentence summary of what
   each file contains (record count, date range, total amounts if visible).
4. Produce an overall batch summary: total records, total amount, date range.

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "batch_label": "<str>",
  "total_files": <int>,
  "by_type": {"<type>": <count>, ...},
  "by_status": {"parsed": <n>, "parse_error": <n>, ...},
  "error_files": [{"filename": "...", "error": "..."}],
  "file_summaries": [{"filename": "...", "summary": "..."}],
  "overall": {
    "total_records": <int or null>,
    "total_amount": <number or null>,
    "date_range": {"from": "...", "to": "..."}
  },
  "narrative": "<two to three sentence plain English summary>"
}

Batch metadata and file contents:
{file_text}

Arguments:
{arguments}
""",
        "output_schema": {
            "type": "object",
            "properties": {
                "batch_label":    {"type": "string"},
                "total_files":    {"type": "integer"},
                "by_type":        {"type": "object"},
                "by_status":      {"type": "object"},
                "narrative":      {"type": "string"},
            },
        },
    },

    {
        "name":         "clean_dataset",
        "display_name": "Clean Dataset",
        "description": (
            "Normalise and clean any tabular dataset: strip empty rows, "
            "standardise date formats, fix float artefacts (trailing .0 on IDs), "
            "deduplicate, and return a clean JSON array of records ready for "
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
                        "List of cleaning operations to apply. "
                        "Options: 'strip_empty', 'deduplicate', 'normalise_dates', "
                        "'fix_ids', 'normalise_numbers'. "
                        "Default: all operations."
                    ),
                    "default": ["strip_empty", "deduplicate", "normalise_dates",
                                "fix_ids", "normalise_numbers"],
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
- fix_ids:           strip trailing ".0" from numeric ID strings (e.g. "1234.0" → "1234")
- normalise_numbers: strip commas and spaces from numeric strings, convert to numbers

Operations requested: {arguments}

Return ONLY valid JSON — no prose, no markdown.

Output format:
{
  "original_count": <int>,
  "cleaned_count": <int>,
  "removed_count": <int>,
  "operations_applied": ["..."],
  "records": [
    { <field>: <cleaned value>, ... }
  ],
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


class Command(BaseCommand):
    help = "Seed or update all built-in ToolDefinitions and domain prompt_transform tools."

    @transaction.atomic
    def handle(self, *args, **options):
        from tools.models import ToolDefinition, UserToolConfig

        created_count = updated_count = 0

        # ── Builtin tools ─────────────────────────────────────────────────────
        for spec in BUILTIN_TOOLS:
            obj, created = ToolDefinition.objects.update_or_create(
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
            if created:
                created_count += 1
                self.stdout.write(f"  [created] builtin: {spec['name']}")
            else:
                updated_count += 1
                self.stdout.write(f"  [updated] builtin: {spec['name']}")

        # ── Prompt transform tools ────────────────────────────────────────────
        for spec in PROMPT_TRANSFORM_TOOLS:
            obj, created = ToolDefinition.objects.update_or_create(
                name=spec["name"],
                defaults={
                    "display_name":      spec["display_name"],
                    "description":       spec["description"],
                    "category":          spec["category"],
                    "tool_type":         "prompt_transform",
                    "handler":           "",   # not used
                    "parameters_schema": spec["parameters_schema"],
                    "is_safe":           spec.get("is_safe", True),
                    "enabled":           True,
                    "created_by":        None,
                },
            )

            # Upsert UserToolConfig (holds the prompt + output schema)
            UserToolConfig.objects.update_or_create(
                tool=obj,
                defaults={
                    "system_prompt": spec["system_prompt"],
                    "output_schema": spec.get("output_schema"),
                    "webhook_url":   "",
                },
            )

            if created:
                created_count += 1
                self.stdout.write(f"  [created] prompt_transform: {spec['name']}")
            else:
                updated_count += 1
                self.stdout.write(f"  [updated] prompt_transform: {spec['name']}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {created_count} created, {updated_count} updated."
            )
        )