# ai_engine/urls.py
from django.urls import path
from .views import (
    AIAnalysisJobListCreateView,
    AIAnalysisJobDetailView,
    AIAnalysisJobRequeueView,
    AIInsightListView,
    AIInsightActionView,
    BatchAIJobListView,
    AIModelsView,
    AIAnalyzeView,
    AIRunAnalysisView,
    AIEngineStatsView,
    AIRecentInsightsView,
)

urlpatterns = [
    # ── Contract endpoints ────────────────────────────────────────────────────
    # GET  /api/ai/models          list available AI models
    path("models/",   AIModelsView.as_view(),  name="ai-models"),
    # POST /api/ai/analyze         run analysis on data
    path("analyze/",  AIAnalyzeView.as_view(), name="ai-analyze"),
    # POST /api/ai/run             run a full agent analysis over uploaded files
    path("run/",      AIRunAnalysisView.as_view(), name="ai-run"),
    # GET  /api/ai/stats           headline stats for the AI Engine page
    path("stats/",    AIEngineStatsView.as_view(), name="ai-stats"),
    # GET  /api/ai/insights/recent recent insights across all jobs
    path("insights/recent/", AIRecentInsightsView.as_view(), name="ai-insights-recent"),

    # ── Jobs ──────────────────────────────────────────────────────────────────
    path("jobs/",                    AIAnalysisJobListCreateView.as_view(), name="ai-job-list-create"),
    path("jobs/<int:pk>/",           AIAnalysisJobDetailView.as_view(),     name="ai-job-detail"),
    path("jobs/<int:pk>/requeue/",   AIAnalysisJobRequeueView.as_view(),    name="ai-job-requeue"),
    path("jobs/<int:job_id>/insights/", AIInsightListView.as_view(),        name="ai-insight-list"),

    # ── Insights ──────────────────────────────────────────────────────────────
    path("insights/<int:pk>/action/", AIInsightActionView.as_view(),        name="ai-insight-action"),

    # ── Batch shortcut ────────────────────────────────────────────────────────
    path("batches/<int:batch_id>/jobs/", BatchAIJobListView.as_view(),      name="ai-batch-job-list"),
]