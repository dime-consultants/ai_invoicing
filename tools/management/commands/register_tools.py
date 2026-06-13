# tools/management/commands/register_tools.py
"""
Management command to register all available tools in the database.

This command:
1. Discovers all tool handler functions
2. Creates or updates ToolDefinition entries in the database
3. Enables all tools for use by the AI

Usage:
    python manage.py register_tools
    python manage.py register_tools --verbose
"""

import json
import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from tools.models import ToolDefinition

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Register all available tools in the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print detailed output",
        )

    def handle(self, *args, **options):
        verbose = options.get("verbose", False)
        self.stdout.write(self.style.SUCCESS("Registering tools..."))

        # Define all available tools with their metadata
        tools_to_register = [
            {
                "name": "detect_file_type",
                "display_name": "Detect File Type",
                "description": "Inspect an uploaded file and determine its type (URA fiscal receipt, Safaricom bill, ACON export, etc.). Returns the detected type and confidence level.",
                "category": "extraction",
                "handler": "tools.handlers.detect_file_type",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the uploaded file to analyze"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "extract_ura_receipts",
                "display_name": "Extract URA Receipts",
                "description": "Parse URA fiscal receipts from a text file. Extracts invoice numbers, amounts, dates, and other key fields. Returns structured data as JSON.",
                "category": "extraction",
                "handler": "tools.handlers.extract_ura_receipts",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the URA receipts file to extract"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "extract_safaricom_bill",
                "display_name": "Extract Safaricom Bill",
                "description": "Parse Safaricom billing documents (PDF or text). Extracts phone numbers, charges, dates, and service details. Returns structured billing data.",
                "category": "extraction",
                "handler": "tools.handlers.extract_safaricom_bill",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the Safaricom bill file to extract"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "clean_acon_export",
                "display_name": "Clean ACON Export",
                "description": "Clean and normalize ACON system export data. Handles missing values, standardizes formats, and validates data integrity. Returns cleaned dataset.",
                "category": "transformation",
                "handler": "tools.handlers.clean_acon_export",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the ACON export file to clean"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "reconcile_ura_vs_acon",
                "display_name": "Reconcile URA vs ACON",
                "description": "Compare URA fiscal receipts against ACON system records. Identifies discrepancies, missing entries, and reconciliation issues. Returns detailed variance report.",
                "category": "reconciliation",
                "handler": "tools.handlers.reconcile_ura_vs_acon",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "ura_file_id": {
                            "type": "integer",
                            "description": "The ID of the URA receipts file"
                        },
                        "acon_file_id": {
                            "type": "integer",
                            "description": "The ID of the ACON export file"
                        }
                    },
                    "required": ["ura_file_id", "acon_file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "flag_anomalies",
                "display_name": "Flag Anomalies",
                "description": "Analyze data for unusual patterns, outliers, and potential errors. Flags suspicious transactions, duplicate entries, and data quality issues.",
                "category": "analysis",
                "handler": "tools.handlers.flag_anomalies",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the file to analyze for anomalies"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
            {
                "name": "generate_report",
                "display_name": "Generate Report",
                "description": "Generate formatted reports from processed data. Produces Excel, PDF, or text summaries suitable for stakeholder review.",
                "category": "report",
                "handler": "tools.handlers.generate_report",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the file to generate report from"
                        },
                        "report_type": {
                            "type": "string",
                            "description": "Type of report: ura_sales, variance_analysis, reconciliation, etc.",
                            "default": "ura_sales"
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": False,
            },
            {
                "name": "summarise_batch",
                "display_name": "Summarise Batch",
                "description": "Generate a summary of all files in an upload batch. Provides statistics, key findings, and overall data quality assessment.",
                "category": "analysis",
                "handler": "tools.handlers.summarise_batch",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "batch_id": {
                            "type": "integer",
                            "description": "The ID of the batch to summarize"
                        }
                    },
                    "required": ["batch_id"]
                },
                "is_safe": True,
            },
            {
                "name": "extract_file_universal",
                "display_name": "Extract File (Universal)",
                "description": "Universal file extraction tool using AI. Intelligently parses any file type (TXT, CSV, XLSX, PDF, DOCX, JSON) and extracts structured data.",
                "category": "extraction",
                "handler": "tools.handlers.extract_file_universal",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "integer",
                            "description": "The ID of the file to extract"
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional context about what to extract from the file",
                            "default": ""
                        }
                    },
                    "required": ["file_id"]
                },
                "is_safe": True,
            },
        ]

        with transaction.atomic():
            created_count = 0
            updated_count = 0

            for tool_data in tools_to_register:
                tool_name = tool_data["name"]
                
                try:
                    tool, created = ToolDefinition.objects.update_or_create(
                        name=tool_name,
                        defaults={
                            "display_name": tool_data["display_name"],
                            "description": tool_data["description"],
                            "category": tool_data["category"],
                            "handler": tool_data["handler"],
                            "parameters_schema": tool_data["parameters_schema"],
                            "is_safe": tool_data["is_safe"],
                            "enabled": True,
                        }
                    )

                    if created:
                        created_count += 1
                        status = self.style.SUCCESS("✓ Created")
                    else:
                        updated_count += 1
                        status = self.style.WARNING("✓ Updated")

                    if verbose:
                        self.stdout.write(f"{status} {tool_name}")

                except Exception as exc:
                    logger.exception(f"Failed to register tool {tool_name}: {exc}")
                    self.stdout.write(
                        self.style.ERROR(f"✗ Failed to register {tool_name}: {exc}")
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ Tool registration complete: {created_count} created, {updated_count} updated"
            )
        )
