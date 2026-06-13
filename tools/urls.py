# tools/urls.py
from django.urls import path
from .views import (
    ToolDefinitionListView,
    ToolDefinitionDetailView,
    ToolCallListView,
    ToolCallDetailView,
    ToolRunView,
    ToolsConvertView,
    ToolsCleanView,
    ToolsValidateView,
)
from .views_active import (
    ActiveToolsView,
    JobActiveToolsView,
    ToolUsageStatsView,
)

urlpatterns = [
    # ── Contract endpoints (/api/tools/...) ───────────────────────────────────
    # POST /api/tools/convert    convert file format → download URL
    path("convert/",  ToolsConvertView.as_view(),  name="tools-convert"),
    # POST /api/tools/clean      clean/process data → download URL + stats
    path("clean/",    ToolsCleanView.as_view(),    name="tools-clean"),
    # POST /api/tools/validate   validate file against schema
    path("validate/", ToolsValidateView.as_view(), name="tools-validate"),

    # ── Tool registry ─────────────────────────────────────────────────────────
    # GET  /api/tools/           list all enabled tools
    path("",          ToolDefinitionListView.as_view(),  name="tool-list"),
    # GET  /api/tools/<id>/      single tool detail + Grok schema
    path("<int:pk>/", ToolDefinitionDetailView.as_view(), name="tool-detail"),

    # ── Tool calls (audit log) ────────────────────────────────────────────────
    path("calls/",          ToolCallListView.as_view(),   name="tool-call-list"),
    path("calls/<int:pk>/", ToolCallDetailView.as_view(), name="tool-call-detail"),

    # POST /api/tools/run/       run a tool directly
    path("run/",      ToolRunView.as_view(), name="tool-run"),

    # ── Active tools (real-time) ──────────────────────────────────────────────
    # GET  /api/tools/active/              list all active/enabled tools
    path("active/",                    ActiveToolsView.as_view(),     name="active-tools"),
    # GET  /api/tools/job/<id>/active/     tools being used for a job
    path("job/<int:job_id>/active/",   JobActiveToolsView.as_view(),  name="job-active-tools"),
    # GET  /api/tools/usage/stats/         tool usage statistics
    path("usage/stats/",               ToolUsageStatsView.as_view(),  name="tool-usage-stats"),
]