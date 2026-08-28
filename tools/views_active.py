# tools/views_active.py
"""
API endpoints to fetch active tools and their usage during processing.
"""
import logging
from django.db import models
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import ToolCall
from .views import _visible_tools_qs
from ai_engine.models import AIAnalysisJob
from users.decorators import authenticated_required

logger = logging.getLogger(__name__)


@api_view(["GET"])
@authenticated_required
def active_tools(request):
    """
    GET /api/tools/active/
    Returns all enabled tools visible to this org, with metadata.
    Supports ?tool_type=webhook|prompt_transform|builtin filter.
    """
    qs = _visible_tools_qs(request.user).order_by("category", "name")

    if tool_type := request.query_params.get("tool_type"):
        qs = qs.filter(tool_type=tool_type)

    data = {
        "tools": [
            {
                "id":          tool.id,
                "name":        tool.display_name or tool.name,
                "snake_name":  tool.name,
                "category":    tool.category,
                "tool_type":   tool.tool_type,
                "description": tool.description,
                "version":     str(tool.version or 1),
                "tags":        ["safe"] if tool.is_safe else ["needs review"],
                "is_custom":   tool.created_by_id is not None,
            }
            for tool in qs
        ],
        "total": qs.count(),
    }
    return Response(data)


@api_view(["GET"])
@authenticated_required
def job_active_tools(request, job_id):
    """
    GET /api/tools/job/<job_id>/active/
    Returns tools used for a specific AI job with their call status.
    """
    try:
        job = AIAnalysisJob.objects.get(pk=job_id, organization=request.user.organization)
    except AIAnalysisJob.DoesNotExist:
        return Response(
            {"error": "Job not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    tool_calls = ToolCall.objects.filter(job=job).select_related("tool")

    active_tools_list = [
        {
            "id":           call.tool.id,
            "name":         call.tool.name,
            "display_name": call.tool.display_name,
            "tool_type":    call.tool.tool_type,
            "status":       call.status,
            "duration_ms":  call.duration_ms,
            "started_at":   call.started_at.isoformat() if call.started_at else None,
            # FIX: was call.updated_at — ToolCall has finished_at, not updated_at
            "finished_at":  call.finished_at.isoformat() if call.finished_at else None,
            "error":        call.error_message if call.status == "error" else None,
        }
        for call in tool_calls
    ]

    return Response({
        "job_id":            job_id,
        "job_status":        job.status,
        "active_tools":      active_tools_list,
        "total_tools_used":  len(active_tools_list),
    })


@api_view(["GET"])
@authenticated_required
def tool_usage_stats(request):
    """
    GET /api/tools/usage/stats/
    Tool usage statistics for the last 30 days.
    """
    from django.utils import timezone
    from datetime import timedelta

    thirty_days_ago = timezone.now() - timedelta(days=30)
    org = request.user.organization

    total_calls = ToolCall.objects.filter(organization=org, created_at__gte=thirty_days_ago).count()

    # FIX: status values are "success" and "error", not "completed" and "failed"
    success_calls = ToolCall.objects.filter(
        organization=org, created_at__gte=thirty_days_ago, status="success"
    ).count()
    failed_calls = ToolCall.objects.filter(
        organization=org, created_at__gte=thirty_days_ago, status="error"
    ).count()
    skipped_calls = ToolCall.objects.filter(
        organization=org, created_at__gte=thirty_days_ago, status="skipped"
    ).count()

    most_used = (
        ToolCall.objects.filter(organization=org, created_at__gte=thirty_days_ago)
        .values("tool__name", "tool__display_name", "tool__tool_type")
        .annotate(count=models.Count("id"))
        .order_by("-count")[:10]
    )

    # Avg duration across successful calls
    from django.db.models import Avg, F, ExpressionWrapper, DurationField
    avg_duration = (
        ToolCall.objects
        .filter(organization=org, created_at__gte=thirty_days_ago, status="success",
                started_at__isnull=False, finished_at__isnull=False)
        .annotate(
            duration=ExpressionWrapper(
                F("finished_at") - F("started_at"),
                output_field=DurationField(),
            )
        )
        .aggregate(avg=Avg("duration"))["avg"]
    )
    avg_ms = (
        int(avg_duration.total_seconds() * 1000)
        if avg_duration else 0
    )

    return Response({
        "period":          "30_days",
        "total_calls":     total_calls,
        "success_calls":   success_calls,
        "failed_calls":    failed_calls,
        "skipped_calls":   skipped_calls,
        "success_rate":    round(success_calls / total_calls * 100, 2) if total_calls else 0,
        "avg_duration_ms": avg_ms,
        "most_used_tools": list(most_used),
    })
