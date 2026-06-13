# tools/management/commands/sync_tools_to_ui.py
"""
Management command to sync tool definitions from the backend to the UI.

This command:
1. Exports all enabled tools from the backend database
2. Generates a JSON file with tool definitions
3. Optionally uploads to a shared location or outputs to stdout
4. Ensures the UI always has the latest tools

Usage:
    python manage.py sync_tools_to_ui
    python manage.py sync_tools_to_ui --output /path/to/output.json
    python manage.py sync_tools_to_ui --upload
"""

import json
import logging
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from tools.models import ToolDefinition

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync tool definitions from backend to UI"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            help="Output file path for tool definitions (default: stdout)",
        )
        parser.add_argument(
            "--upload",
            action="store_true",
            help="Upload tools to a shared location (if configured)",
        )
        parser.add_argument(
            "--validate",
            action="store_true",
            help="Validate tool definitions before syncing",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting tool sync..."))

        try:
            # Fetch all enabled tools
            tools = ToolDefinition.objects.filter(enabled=True).order_by("category", "name")

            if not tools.exists():
                self.stdout.write(self.style.WARNING("No enabled tools found"))
                return

            self.stdout.write(f"Found {tools.count()} enabled tools")

            # Build tool definitions
            tool_definitions = self._build_tool_definitions(tools)

            # Validate if requested
            if options["validate"]:
                self._validate_tools(tool_definitions)
                self.stdout.write(self.style.SUCCESS("✓ All tools validated"))

            # Output or upload
            if options["output"]:
                self._save_to_file(tool_definitions, options["output"])
            elif options["upload"]:
                self._upload_tools(tool_definitions)
            else:
                # Output to stdout as JSON
                self.stdout.write(json.dumps(tool_definitions, indent=2))

            self.stdout.write(self.style.SUCCESS("✓ Tool sync completed successfully"))

        except Exception as exc:
            logger.exception("Tool sync failed: %s", exc)
            raise CommandError(f"Tool sync failed: {exc}")

    def _build_tool_definitions(self, tools):
        """Build a list of tool definitions from database models."""
        definitions = []

        for tool in tools:
            definition = {
                "id": tool.id,
                "name": tool.name,
                "category": tool.category,
                "description": tool.description,
                "enabled": tool.enabled,
                "schema": json.loads(tool.grok_schema) if tool.grok_schema else {},
                "input_format": tool.input_format or "text",
                "output_format": tool.output_format or "text",
                "version": tool.version or "1.0",
                "tags": tool.tags.split(",") if tool.tags else [],
            }
            definitions.append(definition)

        return definitions

    def _validate_tools(self, tools):
        """Validate tool definitions."""
        required_fields = {"id", "name", "category", "description"}

        for tool in tools:
            missing = required_fields - set(tool.keys())
            if missing:
                raise ValueError(f"Tool {tool.get('name', 'unknown')} missing fields: {missing}")

            # Validate schema
            if tool.get("schema") and not isinstance(tool["schema"], dict):
                raise ValueError(f"Tool {tool['name']} has invalid schema")

    def _save_to_file(self, tools, output_path):
        """Save tool definitions to a file."""
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w") as f:
            json.dump(tools, f, indent=2)

        self.stdout.write(self.style.SUCCESS(f"✓ Tools saved to {output_file}"))

    def _upload_tools(self, tools):
        """Upload tools to a shared location."""
        # This is a placeholder for uploading to S3, a shared volume, or an API endpoint
        # Implement based on your infrastructure setup

        self.stdout.write(
            self.style.WARNING(
                "Upload functionality not yet implemented. "
                "Use --output to save to a file instead."
            )
        )
