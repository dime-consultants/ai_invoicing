# ai_engine/urls.py
from django.urls import path
from .views import (
    ai_job_list_create,
    ai_job_detail,
    ai_job_requeue,
    ai_insight_list,
    ai_insight_action,
    batch_ai_job_list,
    ai_models,
    ai_analyze,
    ai_run_analysis,
    ai_engine_stats,
    ai_recent_insights,
)

urlpatterns = [
    # ── Contract endpoints ────────────────────────────────────────────────────
    # GET  /api/ai/models          list available AI models
    path("models/",   ai_models,  name="ai-models"),
    # POST /api/ai/analyze         run analysis on data
    path("analyze/",  ai_analyze, name="ai-analyze"),
    # POST /api/ai/run             run a full agent analysis over uploaded files
    path("run/",      ai_run_analysis, name="ai-run"),
    # GET  /api/ai/stats           headline stats for the AI Engine page
    path("stats/",    ai_engine_stats, name="ai-stats"),
    # GET  /api/ai/insights/recent recent insights across all jobs
    path("insights/recent/", ai_recent_insights, name="ai-insights-recent"),

    # ── Jobs ──────────────────────────────────────────────────────────────────
    path("jobs/",                    ai_job_list_create, name="ai-job-list-create"),
    path("jobs/<int:pk>/",           ai_job_detail,       name="ai-job-detail"),
    path("jobs/<int:pk>/requeue/",   ai_job_requeue,      name="ai-job-requeue"),
    path("jobs/<int:job_id>/insights/", ai_insight_list,  name="ai-insight-list"),

    # ── Insights ──────────────────────────────────────────────────────────────
    path("insights/<int:pk>/action/", ai_insight_action, name="ai-insight-action"),

    # ── Batch shortcut ────────────────────────────────────────────────────────
    path("batches/<int:batch_id>/jobs/", batch_ai_job_list, name="ai-batch-job-list"),
]
