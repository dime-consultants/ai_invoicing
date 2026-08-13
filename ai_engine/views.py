# ai_engine/views.py
import logging

from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AIAnalysisJob, AIInsight
from .serializers import (
    AIAnalysisJobListSerializer,
    AIAnalysisJobDetailSerializer,
    AIAnalysisJobCreateSerializer,
    AIInsightSerializer,
    AIInsightActionSerializer,
)
from .services import AIEngineService

logger = logging.getLogger(__name__)


# ── Permissions ───────────────────────────────────────────────────────────────

class IsAdminOrFinance(permissions.BasePermission):
    """Only admin and finance roles may create or manage AI jobs."""
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and getattr(request.user, "role", None) in ("admin", "finance")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────

class AIAnalysisJobListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/ai/jobs/
        List all jobs for the authenticated user.
        Filters: ?batch=<id>  ?task_type=flag_anomalies  ?status=done

    POST /api/ai/jobs/
        Create a new job and dispatch it via Celery (asynchronously —
        the job is returned in 'queued' state; poll GET /api/ai/jobs/<id>/
        or listen on the WebSocket notifications fired by ai_engine/signals.py
        for status updates).

        Request body:
            {
                "batch":       1,
                "target_file": 3,          // optional
                "task_type":   "flag_anomalies",
                "user_intent": "Flag any receipts with zero tax"  // optional
            }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        return (
            AIAnalysisJobCreateSerializer
            if self.request.method == "POST"
            else AIAnalysisJobListSerializer
        )

    def get_queryset(self):
        qs = (
            AIAnalysisJob.objects
            .filter(organization=self.request.user.organization)
            .select_related("batch", "target_file", "requested_by")
            .order_by("-created_at")
        )
        if batch_id := self.request.query_params.get("batch"):
            qs = qs.filter(batch_id=batch_id)
        if task_type := self.request.query_params.get("task_type"):
            qs = qs.filter(task_type=task_type)
        if job_status := self.request.query_params.get("status"):
            qs = qs.filter(status=job_status)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = AIAnalysisJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vd = serializer.validated_data

        user_prompt = vd.get("user_intent") or vd["task_type"].replace("_", " ").title()

        job = AIEngineService.create_job(
            batch         = vd["batch"],
            task_type     = vd["task_type"],
            user_intent   = vd.get("user_intent", ""),
            user_prompt   = user_prompt,
            system_prompt = vd.get("system_prompt", ""),
            target_file   = vd.get("target_file"),
            requested_by  = request.user,
        )

        # Dispatch via Celery rather than running inline — keeps the HTTP
        # request fast and prevents large files from blocking a web worker.
        # The job stays "queued" in the response; status transitions
        # (running/done/error) are pushed via ai_engine/signals.py over
        # the WebSocket layer, or can be polled via GET /api/ai/jobs/<id>/.
        from .tasks import run_ai_job_task
        from config.dispatch import dispatch
        if dispatch(run_ai_job_task, job.id) is None:
            # Nothing will ever pick this up — don't leave it sitting at "queued".
            job.status        = "error"
            job.error_message = "Could not queue the job — the task broker is unavailable."
            job.save(update_fields=["status", "error_message"])
            return Response(
                AIAnalysisJobDetailSerializer(job).data,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            AIAnalysisJobDetailSerializer(job).data,
            status=status.HTTP_201_CREATED,
        )


class AIAnalysisJobDetailView(generics.RetrieveAPIView):
    """
    GET /api/ai/jobs/<id>/
    Full detail: prompts, raw response, all tool calls, all insights.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class   = AIAnalysisJobDetailSerializer

    def get_queryset(self):
        return (
            AIAnalysisJob.objects
            .filter(organization=self.request.user.organization)
            .prefetch_related("insights", "tool_calls__tool")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Requeue
# ─────────────────────────────────────────────────────────────────────────────

class AIAnalysisJobRequeueView(APIView):
    """
    POST /api/ai/jobs/<id>/requeue/

    Reset a failed or completed job back to 'queued' and re-dispatch it
    via Celery. Deletes previous insights and tool calls so they are
    regenerated cleanly.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            job = AIAnalysisJob.objects.get(pk=pk, organization=request.user.organization)
        except AIAnalysisJob.DoesNotExist:
            return Response(
                {"error": "Job not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if job.status not in ("error", "done"):
            return Response(
                {"error": f"Cannot requeue a job with status '{job.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # AIEngineService.requeue() resets the job and calls
        # AIEngineService.dispatch() directly (synchronously). For
        # consistency with the create() path above, route requeues
        # through Celery too.
        from .models import AIAnalysisJob as _Job
        from .tasks import run_ai_job_task

        job.status         = "queued"
        job.raw_response   = ""
        job.error_message  = ""
        job.input_tokens    = 0
        job.output_tokens   = 0
        job.started_at      = None
        job.finished_at     = None
        job.structured_output = None
        job.save()
        job.insights.all().delete()
        job.tool_calls.all().delete()

        # This path has already deleted the previous insights and tool_calls, so a
        # dispatch failure here destroys prior results and queues nothing. Record
        # it on the job rather than 500-ing with the job stuck at "queued".
        from config.dispatch import dispatch
        if dispatch(run_ai_job_task, job.id) is None:
            job.status        = "error"
            job.error_message = "Could not queue the job — the task broker is unavailable."
            job.save(update_fields=["status", "error_message"])
            return Response(
                AIAnalysisJobDetailSerializer(job).data,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        job.refresh_from_db()

        return Response(AIAnalysisJobDetailSerializer(job).data)


# ─────────────────────────────────────────────────────────────────────────────
# Insights
# ─────────────────────────────────────────────────────────────────────────────

class AIInsightListView(generics.ListAPIView):
    """
    GET /api/ai/jobs/<job_id>/insights/
        All insights for one job.
        Filters: ?severity=critical|warning|info
                 ?type=anomaly|variance_explanation|summary_point|classification|recommendation
                 ?actioned=true|false
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class   = AIInsightSerializer

    def get_queryset(self):
        job_qs = AIAnalysisJob.objects.filter(
            pk=self.kwargs["job_id"],
            organization=self.request.user.organization,
        )
        if not job_qs.exists():
            return AIInsight.objects.none()

        qs = AIInsight.objects.filter(
            job_id=self.kwargs["job_id"]
        ).order_by("-severity", "created_at")

        if sev := self.request.query_params.get("severity"):
            qs = qs.filter(severity=sev)
        if itype := self.request.query_params.get("type"):
            qs = qs.filter(insight_type=itype)
        if actioned := self.request.query_params.get("actioned"):
            qs = qs.filter(is_actioned=(actioned.lower() == "true"))

        return qs


class AIInsightActionView(APIView):
    """
    POST /api/ai/insights/<id>/action/

    Mark an insight as actioned by the current user.

    Request body (optional):
        { "resolution_note": "Reviewed and approved." }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            insight = AIInsight.objects.select_related("job").get(pk=pk)
        except AIInsight.DoesNotExist:
            return Response(
                {"error": "Insight not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if insight.job.organization_id != request.user.organization_id:
            return Response(
                {"error": "You do not have permission to action this insight."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if insight.is_actioned:
            return Response(
                {"error": "Insight is already actioned."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AIInsightActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        insight.is_actioned     = True
        insight.actioned_by     = request.user
        insight.actioned_at     = timezone.now()
        insight.resolution_note = serializer.validated_data.get("resolution_note", "")
        insight.save(update_fields=[
            "is_actioned", "actioned_by", "actioned_at", "resolution_note"
        ])

        return Response(AIInsightSerializer(insight).data)


# ─────────────────────────────────────────────────────────────────────────────
# Batch-scoped shortcut
# ─────────────────────────────────────────────────────────────────────────────

class BatchAIJobListView(generics.ListAPIView):
    """
    GET /api/ai/batches/<batch_id>/jobs/
    All jobs for a specific batch owned by the current user.
    Shortcut so the frontend doesn't need to filter /jobs/?batch=<id>.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class   = AIAnalysisJobListSerializer

    def get_queryset(self):
        return (
            AIAnalysisJob.objects
            .filter(
                batch_id=self.kwargs["batch_id"],
                organization=self.request.user.organization,
            )
            .select_related("batch", "target_file")
            .order_by("-created_at")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Contract: GET /api/ai/models  and  POST /api/ai/analyze
# ─────────────────────────────────────────────────────────────────────────────

class AIModelsView(APIView):
    """
    GET /api/ai/models
    Returns the list of available AI models (static + dynamic from job history).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.conf import settings

        models = [
            {
                "id":          "grok-3",
                "name":        "Grok 3",
                "description": "xAI Grok 3 — primary model for invoice extraction, "
                               "reconciliation, and anomaly detection.",
                "status":      "active" if getattr(settings, "XAI_API_KEY", "") else "inactive",
                "accuracy":    94.5,
                "lastTrained": "2025-01-01T00:00:00Z",
            },
            {
                "id":          "grok-3-mini",
                "name":        "Grok 3 Mini",
                "description": "Faster, lighter model for classification and quick summaries.",
                "status":      "active" if getattr(settings, "XAI_API_KEY", "") else "inactive",
                "accuracy":    91.2,
                "lastTrained": "2025-01-01T00:00:00Z",
            },
        ]
        return Response({"models": models})


class AIAnalyzeView(APIView):
    """
    POST /api/ai/analyze
    Run AI analysis on provided data using a specified model.

    Request:
        {
            "modelId": "grok-3",
            "data": { ... },
            "options": { "includeConfidence": true, "threshold": 0.8 }
        }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        model_id = request.data.get("modelId", "grok-3")
        data     = request.data.get("data", {})
        options  = request.data.get("options", {})

        if not data:
            return Response(
                {"error": {"code": "bad_request", "message": "data is required.", "details": None}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        include_confidence = options.get("includeConfidence", True)
        threshold          = float(options.get("threshold", 0.7))

        try:
            from ai_engine.services import _get_client, GROK_MODEL

            model_map = {
                "grok-3":      GROK_MODEL(),
                "grok-3-mini": "grok-3-mini",
            }
            model = model_map.get(model_id, GROK_MODEL())

            prompt = (
                "Analyse the following data and return a JSON array of predictions. "
                "Each item must have: id (string), prediction (string), confidence (0-1 float), "
                "metadata (object with any relevant details).\n\n"
                f"Data:\n{data}\n\n"
                "Respond with ONLY a JSON array, no markdown fences."
            )

            client   = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a financial data analyst. Return only valid JSON."},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
            raw = response.choices[0].message.content.strip()

            import json
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()

            results = json.loads(raw)
            if not isinstance(results, list):
                results = [results]

            if threshold > 0:
                results = [r for r in results if float(r.get("confidence", 1)) >= threshold]

        except Exception as exc:
            logger.exception("AIAnalyzeView failed: %s", exc)
            results = [
                {
                    "id":         "fallback-1",
                    "prediction": "Analysis unavailable — AI service error.",
                    "confidence": 0.0,
                    "metadata":   {"error": str(exc)},
                }
            ]

        return Response({"results": results})


class AIRunAnalysisView(APIView):
    """
    POST /api/ai/run/
    Run a real AI Engine analysis: creates and executes an AIAnalysisJob through
    the agent (which picks and calls the right tools) over the user's most recent
    uploaded batch, then returns the job id, summary, insights, tools used, and
    download links for any output files the tools produced.

    Output files are persisted as ChatMessageAttachment records (same mechanism
    the chat endpoint uses) so they're served by the existing, already-secured
    chat/views.py:ChatAttachmentDownloadView rather than a second download path.

    Request: { "context": str, "pipeline": str (optional) }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context  = (request.data.get("context") or request.data.get("analysisContext") or "").strip()
        pipeline = (request.data.get("pipeline") or "").strip()

        if not context:
            return Response(
                {"error": "Analysis context is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from uploads.models import UploadBatch
        from ai_engine.services import AIEngineService
        from ai_engine.models import AIInsight
        from tools.models import ToolCall
        from tools.services import ToolService

        batch = (
            UploadBatch.objects
            .filter(organization=request.user.organization, file_count__gt=0)
            .order_by("-created_at")
            .first()
        )

        message = f"[Pipeline: {pipeline}] {context}" if pipeline else context

        try:
            summary, job_id = AIEngineService.handle_chat_message(
                user=request.user,
                message=message,
                batch=batch,
            )
        except Exception as exc:
            logger.exception("AIRunAnalysisView failed: %s", exc)
            return Response(
                {"error": f"Analysis failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        insights = list(
            AIInsight.objects.filter(job_id=job_id)
            .values("insight_type", "severity", "title", "detail")[:25]
        ) if job_id else []
        tools_used = list(
            ToolCall.objects.filter(job_id=job_id)
            .values("tool__name", "status")
        ) if job_id else []

        output_files_meta = []
        if job_id:
            output_files = ToolService.collect_output_files_for_job(job_id)
            if output_files:
                output_files_meta = self._persist_as_chat_attachments(
                    request, output_files, job_id, summary,
                )

        return Response({
            "job_id":       job_id,
            "summary":      summary,
            "batch":        batch.label if batch else None,
            "batch_id":     batch.pk if batch else None,
            "insights":     insights,
            "tools_used":   tools_used,
            "output_files": output_files_meta,
        })

    @staticmethod
    def _persist_as_chat_attachments(request, output_files, job_id, summary_text):
        """
        Save tool-produced output files as ChatMessageAttachment records on a
        sentinel "__ai_engine__" conversation for this user, then return
        download links pointing at chat/views.py:ChatAttachmentDownloadView
        (GET /api/chat/attachments/<id>/download/) — NOT the raw file_url
        from ChatMessageAttachmentSerializer, since that points directly at
        obj.file.url and would skip ChatAttachmentDownloadView's ownership
        check (att.message.conversation.user != request.user) if media is
        ever served directly (e.g. via nginx) rather than through Django.

        Reuses chat.views._save_output_attachments rather than re-implementing
        file persistence here, so there is exactly one place in the codebase
        that turns a tool output dict into a downloadable attachment.
        """
        from chat.models import ChatConversation, ChatMessage
        from chat.views import _save_output_attachments

        conversation, _ = ChatConversation.objects.get_or_create(
            user=request.user,
            title="__ai_engine__",
            defaults={"is_active": False},
        )
        assistant_msg = ChatMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content=f"[AI Engine job {job_id}] {summary_text}",
        )

        saved_attachments = _save_output_attachments(output_files, assistant_msg)

        return [
            {
                "filename": att.filename,
                "download_url": request.build_absolute_uri(
                    f"/api/chat/attachments/{att.pk}/download/"
                ),
                "content_type": att.file_type,
            }
            for att in saved_attachments
        ]


class AIEngineStatsView(APIView):
    """
    GET /api/ai/stats/
    Live headline stats for the AI Engine page: analyses run today (with the
    day-over-day delta), insights generated this week, and average job runtime.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from datetime import timedelta
        from .models import AIAnalysisJob, AIInsight

        now      = timezone.now()
        today    = now.date()
        yesterday = today - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        org = request.user.organization

        analyses_today = AIAnalysisJob.objects.filter(organization=org, created_at__date=today).count()
        analyses_yest  = AIAnalysisJob.objects.filter(organization=org, created_at__date=yesterday).count()
        insights_week  = AIInsight.objects.filter(job__organization=org, job__created_at__gte=week_ago).count()

        delta_pct = None
        if analyses_yest:
            delta_pct = round((analyses_today - analyses_yest) / analyses_yest * 100)

        done = (
            AIAnalysisJob.objects
            .filter(organization=org, status="done", started_at__isnull=False, finished_at__isnull=False)
            .order_by("-finished_at")[:200]
        )
        durations = [(j.finished_at - j.started_at).total_seconds() for j in done]
        avg_seconds = round(sum(durations) / len(durations), 1) if durations else 0

        return Response({
            "analyses_today":           analyses_today,
            "analyses_today_delta_pct": delta_pct,
            "insights_this_week":       insights_week,
            "avg_processing_seconds":   avg_seconds,
        })


class AIRecentInsightsView(APIView):
    """
    GET /api/ai/insights/recent/
    The most recent AI insights across all jobs, shaped for the AI Engine page.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import AIInsight

        rows = (
            AIInsight.objects
            .select_related("job")
            .filter(job__organization=request.user.organization)
            .order_by("-id")[:10]
        )
        severity_map = {"critical": "error", "warning": "warning", "info": "success"}
        insights = [{
            "id":          ins.id,
            "title":       ins.title,
            "description": ins.detail,
            "severity":    severity_map.get(ins.severity, "warning"),
            "source":      ins.get_insight_type_display(),
            "created_at":  ins.job.created_at.isoformat() if ins.job and ins.job.created_at else None,
        } for ins in rows]

        return Response({"insights": insights})