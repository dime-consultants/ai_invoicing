# chat/management/commands/init_workflows.py
"""
python manage.py init_workflows
python manage.py init_workflows --reset

Seeds default Workflow records used by the chat sidebar and agent routing.

IMPORTANT: The tool names in `steps` MUST match ToolDefinition.name values
seeded by `python manage.py seed_tools`. The agent validates available tools
against the DB — unknown names silently produce zero tool calls.

Current valid tool names (from seed_tools):
  Builtin:          read_file, detect_file_type, write_xlsx, run_python, call_webhook
  Prompt transform: extract_invoice_data, flag_anomalies, reconcile_datasets,
                    summarise_batch, clean_dataset
"""

from django.core.management.base import BaseCommand
from chat.models import Workflow


class Command(BaseCommand):
    help = "Initialize (or refresh) default workflows for the chat interface."

    WORKFLOWS = [
        # ── Extraction workflows ───────────────────────────────────────────────
        {
            "name":          "Invoice Data Extraction",
            "description":   "Detect the file type, read its content, extract all invoice "
                             "records into structured JSON, and produce a formatted Excel file.",
            "workflow_type": "extraction",
            "steps":         [
                "detect_file_type",
                "read_file",
                "extract_invoice_data",
                "write_xlsx",
            ],
            "system_prompt_prefix": (
                "Focus on extracting every record from the uploaded file. "
                "Normalise all numbers, dates, and IDs. "
                "After extraction call write_xlsx to produce a downloadable report."
            ),
            "is_default": True,
        },

        # ── Reconciliation workflows ───────────────────────────────────────────
        {
            "name":          "Dataset Reconciliation",
            "description":   "Upload two files and reconcile them on a shared key. "
                             "Returns matched rows, variances, and missing entries.",
            "workflow_type": "reconciliation",
            "steps":         [
                "detect_file_type",
                "read_file",
                "extract_invoice_data",
                "reconcile_datasets",
                "write_xlsx",
            ],
            "system_prompt_prefix": (
                "The user wants to reconcile two datasets. "
                "Extract records from both files, then call reconcile_datasets with "
                "file_id_a and file_id_b. Finish by writing the variance report to xlsx."
            ),
            "is_default": True,
        },

        # ── Anomaly detection ──────────────────────────────────────────────────
        {
            "name":          "Anomaly Detection",
            "description":   "Scan invoice or transaction data for duplicates, zero values, "
                             "outliers, missing tax, and round-number estimates.",
            "workflow_type": "anomaly_detection",
            "steps":         [
                "detect_file_type",
                "read_file",
                "extract_invoice_data",
                "flag_anomalies",
                "write_xlsx",
            ],
            "system_prompt_prefix": (
                "Focus on finding data quality problems. "
                "Extract records first, then call flag_anomalies. "
                "Write a report with the flagged rows highlighted."
            ),
            "is_default": True,
        },

        # ── Data cleaning ──────────────────────────────────────────────────────
        {
            "name":          "Data Cleaning",
            "description":   "Normalise and clean any tabular dataset: strip empty rows, "
                             "fix date formats and ID float artefacts, deduplicate, "
                             "then produce a clean Excel file.",
            "workflow_type": "data_cleaning",
            "steps":         [
                "detect_file_type",
                "read_file",
                "clean_dataset",
                "write_xlsx",
            ],
            "system_prompt_prefix": (
                "The user wants their data cleaned and normalised. "
                "Call clean_dataset with all operations enabled, "
                "then write_xlsx with the cleaned records."
            ),
            "is_default": True,
        },

        # ── Batch summary ──────────────────────────────────────────────────────
        {
            "name":          "Batch Summary",
            "description":   "Produce a plain-English and structured summary of all files "
                             "in the current upload batch.",
            "workflow_type": "batch_summary",
            "steps":         [
                "detect_file_type",
                "read_file",
                "summarise_batch",
            ],
            "system_prompt_prefix": (
                "Give the user a clear overview of their batch. "
                "Call summarise_batch and report the narrative plus any error files."
            ),
            "is_default": True,
        },

        # ── Report generation ──────────────────────────────────────────────────
        {
            "name":          "Financial Report Generation",
            "description":   "Extract invoice data, flag anomalies, and write a "
                             "comprehensive formatted Excel report with totals.",
            "workflow_type": "report_generation",
            "steps":         [
                "detect_file_type",
                "read_file",
                "extract_invoice_data",
                "flag_anomalies",
                "write_xlsx",
            ],
            "system_prompt_prefix": (
                "Produce a comprehensive financial report. "
                "Extract all records, flag any anomalies inline, "
                "then write a formatted xlsx with a totals row."
            ),
            "is_default": True,
        },

        # ── Custom / free-form ─────────────────────────────────────────────────
        {
            "name":          "Custom Analysis",
            "description":   "Full tool access — the AI decides which tools to use "
                             "based on the user's request.",
            "workflow_type": "custom",
            "steps":         [
                "detect_file_type",
                "read_file",
                "extract_invoice_data",
                "flag_anomalies",
                "reconcile_datasets",
                "clean_dataset",
                "summarise_batch",
                "write_xlsx",
                "run_python",
                "call_webhook",
            ],
            "system_prompt_prefix": "",
            "is_default": False,
        },
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing workflows before re-creating them.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            deleted, _ = Workflow.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing workflow(s)."))

        # Validate steps against live ToolDefinition names before writing anything
        from tools.models import ToolDefinition
        registered = set(ToolDefinition.objects.filter(enabled=True).values_list("name", flat=True))

        created_count = updated_count = 0

        for data in self.WORKFLOWS:
            unknown = [s for s in data["steps"] if s not in registered]
            if unknown:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ⚠  '{data['name']}' references unknown tools: {unknown}. "
                        f"Run `seed_tools` first, or fix the steps list."
                    )
                )

            workflow, created = Workflow.objects.update_or_create(
                name=data["name"],
                defaults={
                    "description":         data["description"],
                    "workflow_type":       data["workflow_type"],
                    "steps":               data["steps"],
                    "system_prompt_prefix": data.get("system_prompt_prefix", ""),
                    "is_default":          data["is_default"],
                    "enabled":             True,
                },
            )

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"  ✔  Created : {workflow.name}"))
            else:
                updated_count += 1
                self.stdout.write(self.style.WARNING(f"  ↺  Updated : {workflow.name}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone — {created_count} created, {updated_count} updated."
            )
        )