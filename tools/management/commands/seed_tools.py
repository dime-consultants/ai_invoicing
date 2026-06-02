# tools/management/commands/seed_tools.py
"""
python manage.py seed_tools

Creates or updates ToolDefinition rows for every handler in tools/handlers.py.
Run this once after initial migration and whenever you add a new handler.
"""
from django.core.management.base import BaseCommand
from tools.models import ToolDefinition

TOOLS = [
    {
        "name":         "detect_file_type",
        "display_name": "Detect File Type",
        "description":  (
            "Inspect an uploaded file and determine its document type. "
            "Returns detected_type (e.g. 'ura_fiscal_receipt', 'safaricom_bill', "
            "'acon_export', 'unknown') and a confidence level. "
            "Always call this first when you receive a new file_id."
        ),
        "category": "utility",
        "handler":  "tools.handlers.detect_file_type",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile to inspect.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "extract_ura_receipts",
        "display_name": "Extract URA Fiscal Receipts",
        "description":  (
            "Parse a URA fiscal receipt .txt file (detected_type='ura_fiscal_receipt'). "
            "Extracts every FISCAL RECEIPT and CREDIT NOTE block: "
            "CU Invoice Number, Date, Time, Total (UGX), Taxes (UGX), Entry Type. "
            "Saves an .xlsx output file and returns a preview of up to 5 rows."
        ),
        "category": "extraction",
        "handler":  "tools.handlers.extract_ura_receipts",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile (.txt) to parse.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "extract_safaricom_bill",
        "display_name": "Extract Safaricom Bill",
        "description":  (
            "Extract line items from a Safaricom monthly telephone bill PDF "
            "(detected_type='safaricom_bill'). "
            "Returns: Name, Reference NO., Invoice NO., Net Amount, VAT, Excise, Billed Amount. "
            "Saves an .xlsx output file."
        ),
        "category": "extraction",
        "handler":  "tools.handlers.extract_safaricom_bill",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile (.pdf) to parse.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "clean_acon_export",
        "display_name": "Clean ACON Export",
        "description":  (
            "Load an ACON sales invoice .xlsx export, strip empty rows, "
            "normalise column values, and return a cleaned dataset. "
            "Use this for files where detected_type='acon_export'."
        ),
        "category": "transformation",
        "handler":  "tools.handlers.clean_acon_export",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile (.xlsx) to clean.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "reconcile_ura_vs_acon",
        "display_name": "Reconcile URA vs ACON",
        "description":  (
            "Cross-reference URA fiscal receipt records against ACON sales export records. "
            "Matches on CU Invoice Number vs ACON item/invoice number. "
            "Returns matched count, unmatched count, variance rows, "
            "and saves a variance .xlsx report. "
            "Requires both a URA .txt file_id and an ACON .xlsx file_id."
        ),
        "category": "reconciliation",
        "handler":  "tools.handlers.reconcile_ura_vs_acon",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "ura_file_id": {
                    "type":        "integer",
                    "description": "PK of the URA fiscal receipt UploadedFile (.txt).",
                },
                "acon_file_id": {
                    "type":        "integer",
                    "description": "PK of the ACON export UploadedFile (.xlsx).",
                },
            },
            "required": ["ura_file_id", "acon_file_id"],
        },
    },
    {
        "name":         "flag_anomalies",
        "display_name": "Flag Anomalies",
        "description":  (
            "Scan invoice/receipt data for anomalies: duplicate CU numbers, "
            "unusually large totals (>3 std deviations), round-number estimates, "
            "zero-value entries, and fiscal receipts with zero tax. "
            "Returns a list of anomalies with severity (critical/warning/info)."
        ),
        "category": "analysis",
        "handler":  "tools.handlers.flag_anomalies",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile to scan.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "generate_report",
        "display_name": "Generate Report",
        "description":  (
            "Generate a formatted .xlsx report for a processed file. "
            "report_type options: "
            "'ura_sales' (receipts with totals, tax, net — default), "
            "'safaricom_dept' (lines by department), "
            "'variance_summary' (cross-check variances), "
            "'acon_summary' (ACON breakdown). "
            "Returns the output filename and record count."
        ),
        "category": "report",
        "handler":  "tools.handlers.generate_report",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type":        "integer",
                    "description": "PK of the UploadedFile to report on.",
                },
                "report_type": {
                    "type":        "string",
                    "enum":        ["ura_sales", "safaricom_dept", "variance_summary", "acon_summary"],
                    "description": "Type of report to generate. Defaults to 'ura_sales'.",
                    "default":     "ura_sales",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name":         "summarise_batch",
        "display_name": "Summarise Batch",
        "description":  (
            "Return a structured summary of an UploadBatch: "
            "file count, detected types, parse status breakdown, "
            "and any files that failed to parse. "
            "Use this to give the user an overview of what was uploaded."
        ),
        "category": "analysis",
        "handler":  "tools.handlers.summarise_batch",
        "is_safe":  True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "batch_id": {
                    "type":        "integer",
                    "description": "PK of the UploadBatch to summarise.",
                },
            },
            "required": ["batch_id"],
        },
    },
]


class Command(BaseCommand):
    help = "Seed ToolDefinition rows for all registered handlers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing ToolDefinitions before seeding.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            count, _ = ToolDefinition.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {count} existing tool(s)."))

        created = updated = 0
        for data in TOOLS:
            schema = data.pop("parameters_schema")
            obj, is_new = ToolDefinition.objects.update_or_create(
                name=data["name"],
                defaults={**data, "parameters_schema": schema},
            )
            if is_new:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  ✔ Created : {obj.name}"))
            else:
                updated += 1
                self.stdout.write(self.style.WARNING(f"  ↺ Updated : {obj.name}"))

        self.stdout.write(
            self.style.SUCCESS(f"\nDone — {created} created, {updated} updated.")
        )