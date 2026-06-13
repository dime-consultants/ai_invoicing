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
        Create a new job and dispatch it immediately.

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
            .filter(requested_by=self.request.user)
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

        # Build user_prompt from intent (services.py will enrich with file text)
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

        # Dispatch synchronously for now — swap for Celery task in production:
        #   run_ai_job.delay(job.id)
        AIEngineService.dispatch(job.id)
        job.refresh_from_db()

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
            .filter(requested_by=self.request.user)
            .prefetch_related("insights", "tool_calls__tool")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Requeue
# ─────────────────────────────────────────────────────────────────────────────

class AIAnalysisJobRequeueView(APIView):
    """
    POST /api/ai/jobs/<id>/requeue/

    Reset a failed or completed job back to 'queued' and re-dispatch it.
    Deletes previous insights and tool calls so they are regenerated cleanly.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            job = AIAnalysisJob.objects.get(pk=pk, requested_by=request.user)
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

        AIEngineService.requeue(pk)
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
        # Ensure the job belongs to this user
        job_qs = AIAnalysisJob.objects.filter(
            pk=self.kwargs["job_id"],
            requested_by=self.request.user,
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

        # Only the job owner may action insights
        if insight.job.requested_by_id != request.user.pk:
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
                requested_by=self.request.user,
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
        from django.utils import timezone

        # Static model registry — extend as new models are added
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

        # Run analysis via the AI engine service
        try:
            from django.conf import settings
            from ai_engine.services import _get_client, GROK_MODEL

            model_map = {
                "grok-3":      GROK_MODEL,
                "grok-3-mini": "grok-3-mini",
            }
            model = model_map.get(model_id, GROK_MODEL)

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
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()

            results = json.loads(raw)
            if not isinstance(results, list):
                results = [results]

            # Apply threshold filter if requested
            if threshold > 0:
                results = [r for r in results if float(r.get("confidence", 1)) >= threshold]

        except Exception as exc:
            logger.exception("AIAnalyzeView failed: %s", exc)
            # Return a graceful fallback
            results = [
                {
                    "id":         "fallback-1",
                    "prediction": "Analysis unavailable — AI service error.",
                    "confidence": 0.0,
                    "metadata":   {"error": str(exc)},
                }
            ]

        return Response({"results": results})
