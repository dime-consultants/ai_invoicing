# chat/management/commands/init_workflows.py
"""
python manage.py init_workflows

Seeds default Workflow records used by the chat sidebar and InvoiceAgent routing.

The workflow_type values here MUST match the keys in WORKFLOW_OVERRIDES
(chat/prompts.py) so the agent picks up the right system-prompt prefix.
"""

from django.core.management.base import BaseCommand
from chat.models import Workflow


class Command(BaseCommand):
    help = "Initialize (or refresh) default workflows for the chat interface."

    WORKFLOWS = [
        # ── Conversion workflows ───────────────────────────────────────
        {
            "name":          "URA Fiscal Receipt Processing",
            "description":   "Parse URA .txt fiscal receipts, extract all receipt blocks, "
                             "and produce a structured Excel file plus a URA sales report.",
            "workflow_type": "ura_processing",
            "steps":         ["txt_to_xlsx", "validate", "ura_sales_report"],
            "is_default":    True,
        },
        {
            "name":          "Safaricom Bill Processing",
            "description":   "Extract line items from Safaricom monthly PDF bills and "
                             "produce a department-level billing report.",
            "workflow_type": "safaricom_processing",
            "steps":         ["pdf_to_xlsx", "extract_lines", "safaricom_dept_report"],
            "is_default":    True,
        },
        {
            "name":          "ACON Sales Invoice Processing",
            "description":   "Clean and normalise ACON .xlsx exports, verify vatable/non-vatable "
                             "splits, and produce an ACON reconciliation report.",
            "workflow_type": "acon_processing",
            "steps":         ["xlsx_clean", "validate", "acon_reconciliation_report"],
            "is_default":    True,
        },
        # ── Reconciliation workflows ───────────────────────────────────
        {
            "name":          "Quick Reconciliation",
            "description":   "Reconcile URA receipts against ACON sales records, "
                             "flag variances, and generate a variance summary report.",
            "workflow_type": "reconciliation",
            "steps":         ["txt_to_xlsx", "ura_vs_acon", "variance_summary_report"],
            "is_default":    True,
        },
        # ── Analysis workflows ─────────────────────────────────────────
        {
            "name":          "Line Item Classification",
            "description":   "Convert the uploaded file, then classify every expense line "
                             "into a cost category using AI.",
            "workflow_type": "classification",
            "steps":         ["txt_to_xlsx", "classify_lines", "ura_sales_report"],
            "is_default":    True,
        },
        # ── Report workflows ───────────────────────────────────────────
        {
            "name":          "Financial Report Generation",
            "description":   "Convert the uploaded file and immediately produce a comprehensive "
                             "financial summary report with totals and a batch overview.",
            "workflow_type": "report_generation",
            "steps":         ["txt_to_xlsx", "summarise_batch", "ura_sales_report"],
            "is_default":    True,
        },
        # ── Full pipeline ──────────────────────────────────────────────
        {
            "name":          "Full Pipeline",
            "description":   "Run the complete end-to-end pipeline: convert → reconcile → "
                             "AI analysis → report.  Returns multiple output files.",
            "workflow_type": "full_pipeline",
            "steps":         [
                "txt_to_xlsx",
                "ura_vs_acon",
                "flag_anomalies",
                "variance_summary_report",
                "ura_sales_report",
            ],
            "is_default":    False,
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
            self.stdout.write(
                self.style.WARNING(f"Deleted {deleted} existing workflow(s).")
            )

        created_count = 0
        updated_count = 0

        for data in self.WORKFLOWS:
            workflow, created = Workflow.objects.update_or_create(
                name=data["name"],
                defaults={
                    "description":   data["description"],
                    "workflow_type": data["workflow_type"],
                    "steps":         data["steps"],
                    "is_default":    data["is_default"],
                    "enabled":       True,
                },
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  ✔  Created  : {workflow.name}")
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(f"  ↺  Updated  : {workflow.name}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone — {created_count} created, {updated_count} updated."
            )
        )