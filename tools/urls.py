# tools/urls.py
from django.urls import path
from .views import (
    tool_definition_list,
    tool_definition_detail,
    tool_call_list,
    tool_call_detail,
    tool_call_output_download,
    tool_call_report_download,
    tool_run,
    tools_convert,
    tools_clean,
    tools_validate,
    custom_tool_list_create,
    custom_tool_detail,
    custom_tool_test,
)
from .views_active import (
    active_tools,
    job_active_tools,
    tool_usage_stats,
)

urlpatterns = [
    # ── Contract endpoints ────────────────────────────────────────────────────
    path("convert/",  tools_convert,  name="tools-convert"),
    path("clean/",    tools_clean,    name="tools-clean"),
    path("validate/", tools_validate, name="tools-validate"),

    # ── Tool registry ─────────────────────────────────────────────────────────
    # GET  /api/tools/              list all enabled tools (?category=  ?tool_type=)
    path("",          tool_definition_list,   name="tool-list"),
    # GET  /api/tools/<id>/         single tool detail + Grok schema
    path("<int:pk>/", tool_definition_detail, name="tool-detail"),

    # ── Tool calls (audit log) ────────────────────────────────────────────────
    path("calls/",          tool_call_list,   name="tool-call-list"),
    path("calls/<int:pk>/", tool_call_detail, name="tool-call-detail"),
    # GET /api/tools/calls/<id>/output/download/  download this call's output file
    path("calls/<int:pk>/output/download/", tool_call_output_download, name="tool-call-output-download"),
    # GET /api/tools/calls/<id>/report/?filetype=xlsx|csv|pdf|json|txt  generate+download a report from this call's result
    path("calls/<int:pk>/report/", tool_call_report_download, name="tool-call-report-download"),

    # ── Direct run ────────────────────────────────────────────────────────────
    # POST /api/tools/run/    run a tool directly (all three types supported)
    path("run/",      tool_run, name="tool-run"),

    # ── User-defined tools ────────────────────────────────────────────────────
    # GET  /api/tools/custom/           list caller's tools
    # POST /api/tools/custom/           create a new tool (webhook or prompt_transform)
    path("custom/",                    custom_tool_list_create, name="custom-tool-list-create"),
    # GET    /api/tools/custom/<id>/    retrieve
    # PATCH  /api/tools/custom/<id>/    update
    # DELETE /api/tools/custom/<id>/    soft-delete
    path("custom/<int:pk>/",           custom_tool_detail,      name="custom-tool-detail"),
    # POST /api/tools/custom/<id>/test/ test without going through the LLM loop
    path("custom/<int:pk>/test/",      custom_tool_test,        name="custom-tool-test"),

    # ── Active tools (real-time) ──────────────────────────────────────────────
    # GET  /api/tools/active/                    list all active tools (?tool_type=)
    path("active/",                    active_tools,     name="active-tools"),
    # GET  /api/tools/job/<id>/active/           tools used by a specific job
    path("job/<int:job_id>/active/",   job_active_tools, name="job-active-tools"),
    # GET  /api/tools/usage/stats/               30-day usage statistics
    path("usage/stats/",               tool_usage_stats, name="tool-usage-stats"),
]
