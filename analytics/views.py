# analytics/views.py
"""
Views for:
  GET /api/dashboard/stats
  GET /api/dashboard/activity
  GET /api/dashboard/recent-activity
  GET /api/analytics/overview
  GET /api/analytics/charts/<chartType>
  GET /api/reports/
  POST /api/reports/generate
  GET /api/reports/<id>/download
"""
import logging
from datetime import timedelta, date

from django.db.models import Count, Q
from django.http import FileResponse
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Report
from .serializers import ReportSerializer, ReportGenerateSerializer

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _period_start(period: str):
    """Return the start datetime for the given period string."""
    now = timezone.now()
    if period == "day":
        return now - timedelta(days=1)
    if period == "week":
        return now - timedelta(weeks=1)
    if period == "year":
        return now - timedelta(days=365)
    # default: month
    return now - timedelta(days=30)


def _safe_import():
    """Lazy import to avoid circular imports at module load time."""
    from uploads.models import UploadBatch, UploadedFile
    from ai_engine.models import AIAnalysisJob
    from chat.models import ChatConversation, ChatMessage
    return UploadBatch, UploadedFile, AIAnalysisJob, ChatConversation, ChatMessage


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class DashboardStatsView(APIView):
    """GET /api/dashboard/stats"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        UploadBatch, UploadedFile, AIAnalysisJob, _, _ = _safe_import()
        org = request.user.organization

        now      = timezone.now()
        month_ago = now - timedelta(days=30)
        prev_month_start = now - timedelta(days=60)

        # ── Invoices processed ────────────────────────────────────────────────
        current_files  = UploadedFile.objects.filter(batch__organization=org, uploaded_at__gte=month_ago).count()
        previous_files = UploadedFile.objects.filter(
            batch__organization=org,
            uploaded_at__gte=prev_month_start,
            uploaded_at__lt=month_ago,
        ).count()
        files_change = current_files - previous_files
        files_trend  = "up" if files_change > 0 else ("down" if files_change < 0 else "neutral")

        # ── Active workflows (running + queued jobs) ──────────────────────────
        active_jobs = AIAnalysisJob.objects.filter(organization=org, status__in=("queued", "running")).count()
        prev_active = AIAnalysisJob.objects.filter(
            organization=org,
            status__in=("queued", "running"),
            created_at__lt=month_ago,
        ).count()
        jobs_change = active_jobs - prev_active
        jobs_trend  = "up" if jobs_change > 0 else ("down" if jobs_change < 0 else "neutral")

        # ── Accuracy (% of jobs that completed successfully) ──────────────────
        total_jobs   = AIAnalysisJob.objects.filter(organization=org, created_at__gte=month_ago).count()
        done_jobs    = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=month_ago, status="done"
        ).count()
        accuracy     = round((done_jobs / total_jobs * 100) if total_jobs else 0, 1)

        prev_total   = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=prev_month_start, created_at__lt=month_ago
        ).count()
        prev_done    = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=prev_month_start, created_at__lt=month_ago, status="done"
        ).count()
        prev_accuracy = round((prev_done / prev_total * 100) if prev_total else 0, 1)
        acc_change    = round(accuracy - prev_accuracy, 1)
        acc_trend     = "up" if acc_change > 0 else ("down" if acc_change < 0 else "neutral")

        # ── Time saved (rough estimate: 5 min per automated file) ─────────────
        automated_files = UploadedFile.objects.filter(
            batch__organization=org,
            uploaded_at__gte=month_ago,
            parse_status="parsed",
        ).count()
        minutes_saved = automated_files * 5
        if minutes_saved >= 60:
            time_saved_str = f"{minutes_saved // 60}h {minutes_saved % 60}m"
        else:
            time_saved_str = f"{minutes_saved}m"

        prev_automated = UploadedFile.objects.filter(
            batch__organization=org,
            uploaded_at__gte=prev_month_start,
            uploaded_at__lt=month_ago,
            parse_status="parsed",
        ).count()
        time_change = automated_files - prev_automated
        time_trend  = "up" if time_change > 0 else ("down" if time_change < 0 else "neutral")

        return Response({
            "stats": [
                {
                    "title": "Invoices Processed",
                    "value": str(current_files),
                    "change": str(files_change),
                    "changeType": "positive" if files_change > 0 else ("negative" if files_change < 0 else "neutral"),
                    "description": "Files processed this month",
                },
                {
                    "title": "Active Workflows",
                    "value": str(active_jobs),
                    "change": str(jobs_change),
                    "changeType": "positive" if jobs_change > 0 else ("negative" if jobs_change < 0 else "neutral"),
                    "description": "Currently running or queued",
                },
                {
                    "title": "Accuracy",
                    "value": f"{accuracy}%",
                    "change": f"{acc_change:+.1f}%",
                    "changeType": "positive" if acc_change > 0 else ("negative" if acc_change < 0 else "neutral"),
                    "description": "Success rate of completed jobs",
                },
                {
                    "title": "Time Saved",
                    "value": time_saved_str,
                    "change": str(time_change),
                    "changeType": "positive" if time_change > 0 else ("negative" if time_change < 0 else "neutral"),
                    "description": "Automated processing time",
                },
            ],
        })


class DashboardActivityView(APIView):
    """GET /api/dashboard/activity?period=week"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        UploadBatch, UploadedFile, AIAnalysisJob, _, _ = _safe_import()
        org = request.user.organization

        period = request.query_params.get("period", "week")
        since  = _period_start(period)

        # Build daily buckets
        data = []
        now  = timezone.now()
        days = (now - since).days or 1

        for i in range(min(days, 30)):
            day_start = (now - timedelta(days=days - i - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = day_start + timedelta(days=1)

            invoices  = UploadedFile.objects.filter(
                batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end
            ).count()
            automated = UploadedFile.objects.filter(
                batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end,
                parse_status="parsed",
            ).count()

            data.append({
                "name":      day_start.strftime("%b %d"),
                "invoices":  invoices,
                "automated": automated,
            })

        return Response({"data": data})


class DashboardRecentActivityView(APIView):
    """GET /api/dashboard/recent-activity"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        UploadBatch, UploadedFile, AIAnalysisJob, _, ChatMessage = _safe_import()
        org = request.user.organization

        activities = []

        # Recent uploads
        for uf in UploadedFile.objects.filter(batch__organization=org).select_related("batch").order_by("-uploaded_at")[:5]:
            activities.append({
                "id":          f"upload-{uf.pk}",
                "type":        "upload",
                "title":       f"File uploaded: {uf.original_filename}",
                "description": f"Batch: {uf.batch.label}",
                "timestamp":   uf.uploaded_at.isoformat(),
                "status":      "success" if uf.parse_status == "parsed" else (
                    "error" if uf.parse_status == "parse_error" else "pending"
                ),
            })

        # Recent AI jobs
        for job in AIAnalysisJob.objects.filter(organization=org).select_related("batch").order_by("-created_at")[:5]:
            activities.append({
                "id":          f"ai-{job.pk}",
                "type":        "ai",
                "title":       f"AI Job: {job.get_task_type_display()}",
                "description": f"Batch: {job.batch.label}",
                "timestamp":   job.created_at.isoformat(),
                "status":      "success" if job.status == "done" else (
                    "error" if job.status == "error" else "pending"
                ),
            })

        # Recent reports
        for report in Report.objects.filter(organization=org).order_by("-created_at")[:3]:
            activities.append({
                "id":          f"report-{report.pk}",
                "type":        "report",
                "title":       f"Report: {report.name}",
                "description": f"Format: {report.format.upper()}",
                "timestamp":   report.created_at.isoformat(),
                "status":      report.status if report.status in ("success", "pending", "error") else "success",
            })

        # Sort all by timestamp descending
        activities.sort(key=lambda x: x["timestamp"], reverse=True)

        return Response({"activities": activities[:20]})


class DashboardProcessingView(APIView):
    """GET /api/dashboard/processing"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        UploadBatch, UploadedFile, AIAnalysisJob, _, _ = _safe_import()
        org = request.user.organization

        period = request.query_params.get("period", "month")
        since  = _period_start(period)

        # Build daily buckets for processing chart
        data = []
        now  = timezone.now()
        days = (now - since).days or 1

        for i in range(min(days, 30)):
            day_start = (now - timedelta(days=days - i - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = day_start + timedelta(days=1)

            # Count invoices uploaded and reconciled
            invoices  = UploadedFile.objects.filter(
                batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end
            ).count()
            reconciled = UploadedFile.objects.filter(
                batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end,
                parse_status="parsed",
            ).count()

            data.append({
                "name":        day_start.strftime("%b %d"),
                "scrutinized": invoices,
                "flagged":     reconciled,
            })

        return Response({"data": data})


# ─────────────────────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────────────────────

class AnalyticsOverviewView(APIView):
    """GET /api/analytics/overview?period=month"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        UploadBatch, UploadedFile, AIAnalysisJob, _, _ = _safe_import()
        org = request.user.organization

        period = request.query_params.get("period", "month")
        since  = _period_start(period)
        now    = timezone.now()

        # ── Processing ────────────────────────────────────────────────────────
        total_files     = UploadedFile.objects.filter(batch__organization=org, uploaded_at__gte=since).count()
        automated_files = UploadedFile.objects.filter(
            batch__organization=org, uploaded_at__gte=since, parse_status="parsed"
        ).count()
        manual_files    = total_files - automated_files

        # Daily trend for processing
        days = (now - since).days or 1
        processing_trend = []
        for i in range(min(days, 30)):
            day_start = (now - timedelta(days=days - i - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = day_start + timedelta(days=1)
            count = UploadedFile.objects.filter(
                batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end
            ).count()
            processing_trend.append({
                "date":  day_start.strftime("%Y-%m-%d"),
                "value": count,
            })

        # ── Accuracy ──────────────────────────────────────────────────────────
        total_jobs = AIAnalysisJob.objects.filter(organization=org, created_at__gte=since).count()
        done_jobs  = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=since, status="done"
        ).count()
        current_accuracy = round((done_jobs / total_jobs * 100) if total_jobs else 0, 1)

        prev_since = since - (now - since)
        prev_total = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=prev_since, created_at__lt=since
        ).count()
        prev_done  = AIAnalysisJob.objects.filter(
            organization=org, created_at__gte=prev_since, created_at__lt=since, status="done"
        ).count()
        prev_accuracy = round((prev_done / prev_total * 100) if prev_total else 0, 1)

        accuracy_trend = []
        for i in range(min(days, 30)):
            day_start = (now - timedelta(days=days - i - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = day_start + timedelta(days=1)
            d_total = AIAnalysisJob.objects.filter(
                organization=org, created_at__gte=day_start, created_at__lt=day_end
            ).count()
            d_done  = AIAnalysisJob.objects.filter(
                organization=org, created_at__gte=day_start, created_at__lt=day_end, status="done"
            ).count()
            accuracy_trend.append({
                "date":  day_start.strftime("%Y-%m-%d"),
                "value": round((d_done / d_total * 100) if d_total else 0, 1),
            })

        # ── Efficiency ────────────────────────────────────────────────────────
        time_saved_minutes = automated_files * 5
        cost_saved         = round(automated_files * 0.5, 2)  # $0.50 per automated file

        return Response({
            "processing": {
                "total":     total_files,
                "automated": automated_files,
                "manual":    manual_files,
                "trend":     processing_trend,
            },
            "accuracy": {
                "current":  current_accuracy,
                "previous": prev_accuracy,
                "trend":    accuracy_trend,
            },
            "efficiency": {
                "timeSaved": time_saved_minutes,
                "costSaved": cost_saved,
            },
        })


class AnalyticsChartView(APIView):
    """GET /api/analytics/charts/<chartType>"""
    permission_classes = [permissions.IsAuthenticated]

    VALID_CHART_TYPES = {
        "processing-trend",
        "accuracy-trend",
        "category-breakdown",
        "user-activity",
    }

    def get(self, request, chart_type):
        if chart_type not in self.VALID_CHART_TYPES:
            return Response(
                {"error": {"code": "not_found", "message": f"Unknown chart type: {chart_type}", "details": None}},
                status=status.HTTP_404_NOT_FOUND,
            )

        UploadBatch, UploadedFile, AIAnalysisJob, _, _ = _safe_import()
        org = request.user.organization

        period = request.query_params.get("period", "month")
        since  = _period_start(period)
        now    = timezone.now()
        days   = (now - since).days or 1

        if chart_type == "processing-trend":
            data = []
            for i in range(min(days, 30)):
                day_start = (now - timedelta(days=days - i - 1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                day_end = day_start + timedelta(days=1)
                count = UploadedFile.objects.filter(
                    batch__organization=org, uploaded_at__gte=day_start, uploaded_at__lt=day_end
                ).count()
                data.append({"date": day_start.strftime("%Y-%m-%d"), "value": count})
            return Response({"chartType": chart_type, "data": data})

        if chart_type == "accuracy-trend":
            data = []
            for i in range(min(days, 30)):
                day_start = (now - timedelta(days=days - i - 1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                day_end = day_start + timedelta(days=1)
                d_total = AIAnalysisJob.objects.filter(
                    organization=org, created_at__gte=day_start, created_at__lt=day_end
                ).count()
                d_done  = AIAnalysisJob.objects.filter(
                    organization=org, created_at__gte=day_start, created_at__lt=day_end, status="done"
                ).count()
                data.append({
                    "date":  day_start.strftime("%Y-%m-%d"),
                    "value": round((d_done / d_total * 100) if d_total else 0, 1),
                })
            return Response({"chartType": chart_type, "data": data})

        if chart_type == "category-breakdown":
            breakdown = (
                UploadedFile.objects
                .filter(batch__organization=org, uploaded_at__gte=since)
                .values("detected_type")
                .annotate(count=Count("id"))
                .order_by("-count")
            )
            data = [
                {"category": row["detected_type"] or "unknown", "count": row["count"]}
                for row in breakdown
            ]
            return Response({"chartType": chart_type, "data": data})

        if chart_type == "user-activity":
            from users.models import User
            data = []
            for user in User.objects.filter(is_active=True, organization=org)[:10]:
                count = UploadedFile.objects.filter(
                    batch__uploaded_by=user,
                    uploaded_at__gte=since,
                ).count()
                data.append({
                    "user":  user.get_full_name() or user.username,
                    "count": count,
                })
            data.sort(key=lambda x: x["count"], reverse=True)
            return Response({"chartType": chart_type, "data": data})

        return Response({"chartType": chart_type, "data": []})


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────

class ReportListView(generics.ListAPIView):
    """GET /api/reports/"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class   = ReportSerializer

    def get_queryset(self):
        qs = Report.objects.filter(organization=self.request.user.organization)
        report_type = self.request.query_params.get("type")
        if report_type:
            qs = qs.filter(report_type=report_type)
        return qs

    def list(self, request, *args, **kwargs):
        qs    = self.get_queryset()
        page  = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 20))

        total  = qs.count()
        offset = (page - 1) * limit
        page_qs = qs[offset: offset + limit]

        serializer = self.get_serializer(page_qs, many=True)
        return Response({
            "reports": serializer.data,
            "total":   total,
        })


class ReportGenerateView(APIView):
    """POST /api/reports/generate"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ReportGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vd = serializer.validated_data
        report = Report.objects.create(
            name=f"{vd['type'].title()} Report — {timezone.now().strftime('%Y-%m-%d %H:%M')}",
            report_type=vd["type"],
            format=vd["format"],
            parameters=vd.get("parameters", {}),
            requested_by=request.user,
            organization=request.user.organization,
            status="generating",
        )

        # In production, dispatch a Celery task here:
        #   generate_report_task.delay(report.id)
        # For now, mark as ready immediately (placeholder)
        report.status      = "ready"
        report.generated_at = timezone.now()
        report.save(update_fields=["status", "generated_at"])

        return Response(
            {
                "reportId":      report.pk,
                "status":        "queued",
                "estimatedTime": 30,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ReportDownloadView(APIView):
    """GET /api/reports/<id>/download"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            report = Report.objects.get(pk=pk, organization=request.user.organization)
        except Report.DoesNotExist:
            return Response(
                {"error": {"code": "not_found", "message": "Report not found.", "details": None}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if report.status != "ready":
            return Response(
                {"error": {"code": "not_ready", "message": "Report is not ready yet.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not report.file:
            return Response(
                {"error": {"code": "not_found", "message": "Report file not available.", "details": None}},
                status=status.HTTP_404_NOT_FOUND,
            )

        content_types = {
            "pdf":  "application/pdf",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv":  "text/csv",
        }
        ct = content_types.get(report.format, "application/octet-stream")

        response = FileResponse(report.file.open("rb"), content_type=ct, as_attachment=True)
        response["Content-Disposition"] = f'attachment; filename="{report.name}.{report.format}"'
        return response
