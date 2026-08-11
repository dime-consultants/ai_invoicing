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
    CustomToolListCreateView,
    CustomToolDetailView,
    CustomToolTestView,
)
from .views_active import (
    ActiveToolsView,
    JobActiveToolsView,
    ToolUsageStatsView,
)

urlpatterns = [
    # ── Contract endpoints ────────────────────────────────────────────────────
    path("convert/",  ToolsConvertView.as_view(),  name="tools-convert"),
    path("clean/",    ToolsCleanView.as_view(),    name="tools-clean"),
    path("validate/", ToolsValidateView.as_view(), name="tools-validate"),

    # ── Tool registry ─────────────────────────────────────────────────────────
    # GET  /api/tools/              list all enabled tools (?category=  ?tool_type=)
    path("",          ToolDefinitionListView.as_view(),   name="tool-list"),
    # GET  /api/tools/<id>/         single tool detail + Grok schema
    path("<int:pk>/", ToolDefinitionDetailView.as_view(), name="tool-detail"),

    # ── Tool calls (audit log) ────────────────────────────────────────────────
    path("calls/",          ToolCallListView.as_view(),   name="tool-call-list"),
    path("calls/<int:pk>/", ToolCallDetailView.as_view(), name="tool-call-detail"),

    # ── Direct run ────────────────────────────────────────────────────────────
    # POST /api/tools/run/    run a tool directly (all three types supported)
    path("run/",      ToolRunView.as_view(), name="tool-run"),

    # ── User-defined tools ────────────────────────────────────────────────────
    # GET  /api/tools/custom/           list caller's tools
    # POST /api/tools/custom/           create a new tool (webhook or prompt_transform)
    path("custom/",                    CustomToolListCreateView.as_view(), name="custom-tool-list-create"),
    # GET    /api/tools/custom/<id>/    retrieve
    # PATCH  /api/tools/custom/<id>/    update
    # DELETE /api/tools/custom/<id>/    soft-delete
    path("custom/<int:pk>/",           CustomToolDetailView.as_view(),     name="custom-tool-detail"),
    # POST /api/tools/custom/<id>/test/ test without going through the LLM loop
    path("custom/<int:pk>/test/",      CustomToolTestView.as_view(),       name="custom-tool-test"),

    # ── Active tools (real-time) ──────────────────────────────────────────────
    # GET  /api/tools/active/                    list all active tools (?tool_type=)
    path("active/",                    ActiveToolsView.as_view(),     name="active-tools"),
    # GET  /api/tools/job/<id>/active/           tools used by a specific job
    path("job/<int:job_id>/active/",   JobActiveToolsView.as_view(),  name="job-active-tools"),
    # GET  /api/tools/usage/stats/               30-day usage statistics
    path("usage/stats/",               ToolUsageStatsView.as_view(),  name="tool-usage-stats"),
]