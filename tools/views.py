# tools/views.py
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ToolDefinition, ToolCall
from .serializers import ToolDefinitionSerializer, ToolCallSerializer


# ── Permissions ───────────────────────────────────────────────────────────────

class IsAdminOrFinance(permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role in ("admin", "finance")
        )


# ── ToolDefinition ────────────────────────────────────────────────────────────

class ToolDefinitionListView(generics.ListAPIView):
    """
    GET /api/tools/
    List all enabled tools. Finance and admin users only.
    Optional filter: ?category=extraction
    """
    serializer_class   = ToolDefinitionSerializer
    permission_classes = [IsAdminOrFinance]

    def get_queryset(self):
        qs = ToolDefinition.objects.filter(enabled=True).order_by("category", "name")
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        return qs


class ToolDefinitionDetailView(generics.RetrieveAPIView):
    """
    GET /api/tools/<id>/
    Full detail for one tool including the Grok schema.
    """
    serializer_class   = ToolDefinitionSerializer
    permission_classes = [IsAdminOrFinance]
    queryset           = ToolDefinition.objects.filter(enabled=True)


# ── ToolCall ──────────────────────────────────────────────────────────────────

class ToolCallListView(generics.ListAPIView):
    """
    GET /api/tools/calls/
    List tool calls. Supports filtering:
        ?job=<job_id>
        ?tool=<tool_name>
        ?status=success|error|pending|running|skipped
    """
    serializer_class   = ToolCallSerializer
    permission_classes = [IsAdminOrFinance]

    def get_queryset(self):
        qs = ToolCall.objects.select_related("tool", "job").order_by("-created_at")
        if job_id := self.request.query_params.get("job"):
            qs = qs.filter(job_id=job_id)
        if tool_name := self.request.query_params.get("tool"):
            qs = qs.filter(tool__name=tool_name)
        if status_val := self.request.query_params.get("status"):
            qs = qs.filter(status=status_val)
        return qs


class ToolCallDetailView(generics.RetrieveAPIView):
    """
    GET /api/tools/calls/<id>/
    Full detail for one tool call including arguments and result.
    """
    serializer_class   = ToolCallSerializer
    permission_classes = [IsAdminOrFinance]
    queryset           = ToolCall.objects.select_related("tool", "job")


class ToolRunView(APIView):
    """
    POST /api/tools/run/

    Execute a single tool directly (bypasses the LLM loop).
    Useful for testing tools from the admin or from another service.

    Request body:
        {
            "tool_name":  "extract_ura_receipts",
            "arguments":  { "file_id": 42 }
        }

    Response:
        {
            "tool_call_id": 7,
            "result":       { ... }
        }
    """
    permission_classes = [IsAdminOrFinance]

    def post(self, request):
        tool_name = request.data.get("tool_name", "").strip()
        arguments = request.data.get("arguments", {})

        if not tool_name:
            return Response(
                {"error": "tool_name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            tool_def = ToolDefinition.objects.get(name=tool_name, enabled=True)
        except ToolDefinition.DoesNotExist:
            return Response(
                {"error": f"Tool '{tool_name}' not found or not enabled."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not tool_def.is_safe:
            return Response(
                {"error": f"Tool '{tool_name}' is marked unsafe and cannot be run directly."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from datetime import datetime, timezone as tz
        import importlib

        started_at = datetime.now(tz.utc)
        try:
            module_path, _, func_name = tool_def.handler.rpartition(".")
            module  = importlib.import_module(module_path)
            handler = getattr(module, func_name)
            result  = handler(**arguments)
            tc_status = "success" if result.get("ok", True) else "error"
            error_msg = result.get("error", "")
        except Exception as exc:
            result    = {"ok": False, "error": str(exc)}
            tc_status = "error"
            error_msg = str(exc)

        finished_at = datetime.now(tz.utc)

        tc = ToolCall.objects.create(
            tool=tool_def,
            arguments=arguments,
            result=result,
            status=tc_status,
            error_message=error_msg,
            started_at=started_at,
            finished_at=finished_at,
        )

        return Response(
            {"tool_call_id": tc.pk, "result": result},
            status=status.HTTP_200_OK,
        )