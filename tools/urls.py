# tools/urls.py
from django.urls import path
from .views import (
    ToolDefinitionListView,
    ToolDefinitionDetailView,
    ToolCallListView,
    ToolCallDetailView,
    ToolRunView,
)

urlpatterns = [
    # Tool registry
    # GET  /api/tools/                list all enabled tools
    # GET  /api/tools/?category=extraction
    path("", ToolDefinitionListView.as_view(), name="tool-list"),

    # GET  /api/tools/<id>/           single tool detail + Grok schema
    path("<int:pk>/", ToolDefinitionDetailView.as_view(), name="tool-detail"),

    # Tool calls (audit log)
    # GET  /api/tools/calls/          list all calls
    # GET  /api/tools/calls/?job=<id>
    # GET  /api/tools/calls/?tool=extract_ura_receipts
    # GET  /api/tools/calls/?status=error
    path("calls/", ToolCallListView.as_view(), name="tool-call-list"),

    # GET  /api/tools/calls/<id>/     single call detail
    path("calls/<int:pk>/", ToolCallDetailView.as_view(), name="tool-call-detail"),

    # POST /api/tools/run/            run a tool directly (test / admin use)
    path("run/", ToolRunView.as_view(), name="tool-run"),
]