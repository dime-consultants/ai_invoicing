# tools/views_active.py
"""
API endpoint to fetch active tools and their usage during processing.
"""

import logging
from django.db import models
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ToolDefinition, ToolCall
from ai_engine.models import AIAnalysisJob

logger = logging.getLogger(__name__)


class ActiveToolsView(APIView):
    """
    GET /api/tools/active/
    
    Returns a list of tools that are currently enabled and available.
    Includes metadata about each tool.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """Fetch all active/enabled tools."""
        tools = ToolDefinition.objects.filter(enabled=True).order_by("category", "name")

        data = {
            "tools": [
                {
                    "id": tool.id,
                    "name": tool.display_name or tool.name,
                    "category": tool.category,
                    "description": tool.description,
                    "version": str(tool.version or 1),
                    # ToolDefinition has no `tags` field — surface the safety flag
                    # as a tag so the UI still has something meaningful to show.
                    "tags": ["safe"] if tool.is_safe else ["needs review"],
                }
                for tool in tools
            ],
            "total": tools.count(),
        }

        return Response(data)


class JobActiveToolsView(APIView):
    """
    GET /api/tools/job/<job_id>/active/
    
    Returns tools that are currently being used for a specific AI job.
    Useful for showing real-time progress during processing.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, job_id):
        """Fetch tools being used for a specific job."""
        try:
            job = AIAnalysisJob.objects.get(pk=job_id)
        except AIAnalysisJob.DoesNotExist:
            return Response(
                {"error": "Job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Get all tool calls for this job
        tool_calls = ToolCall.objects.filter(job=job).select_related("tool")

        # Build active tools list
        active_tools = []
        for call in tool_calls:
            active_tools.append(
                {
                    "id": call.tool.id,
                    "name": call.tool.name,
                    "status": call.status,  # pending, running, completed, error
                    "started_at": call.created_at.isoformat() if call.created_at else None,
                    "completed_at": call.updated_at.isoformat() if call.updated_at else None,
                    "error": call.error_message if call.status == "error" else None,
                }
            )

        data = {
            "job_id": job_id,
            "job_status": job.status,
            "active_tools": active_tools,
            "total_tools_used": len(active_tools),
        }

        return Response(data)


class ToolUsageStatsView(APIView):
    """
    GET /api/tools/usage/stats/
    
    Returns statistics about tool usage.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """Fetch tool usage statistics."""
        # Get stats for the last 30 days
        from django.utils import timezone
        from datetime import timedelta

        thirty_days_ago = timezone.now() - timedelta(days=30)

        # Count tool calls by status
        total_calls = ToolCall.objects.filter(created_at__gte=thirty_days_ago).count()
        completed_calls = ToolCall.objects.filter(
            created_at__gte=thirty_days_ago, status="completed"
        ).count()
        failed_calls = ToolCall.objects.filter(
            created_at__gte=thirty_days_ago, status="error"
        ).count()

        # Most used tools
        most_used = (
            ToolCall.objects.filter(created_at__gte=thirty_days_ago)
            .values("tool__name")
            .annotate(count=models.Count("id"))
            .order_by("-count")[:10]
        )

        data = {
            "period": "30_days",
            "total_calls": total_calls,
            "completed_calls": completed_calls,
            "failed_calls": failed_calls,
            "success_rate": (
                round((completed_calls / total_calls * 100), 2) if total_calls > 0 else 0
            ),
            "most_used_tools": list(most_used),
        }

        return Response(data)
