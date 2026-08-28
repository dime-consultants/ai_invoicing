# tools/views.py
import logging
from datetime import timedelta
from pathlib import Path

from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from .models import ToolDefinition, ToolCall, UserToolConfig
from .serializers import (
    ToolDefinitionSerializer,
    ToolCallSerializer,
    CustomToolCreateSerializer,
    CustomToolUpdateSerializer,
    CustomToolDetailSerializer,
)
from users.decorators import authenticated_required

logger = logging.getLogger(__name__)


def _visible_tools_qs(user):
    """Built-ins (created_by IS NULL) plus this user's own org's custom tools."""
    return ToolDefinition.objects.filter(enabled=True).filter(
        Q(created_by__isnull=True) | Q(organization=user.organization)
    )


# ── ToolDefinition ────────────────────────────────────────────────────────────

@api_view(["GET"])
@authenticated_required
def tool_definition_list(request):
    """GET /api/tools/ — list all enabled tools visible to this org."""
    qs = _visible_tools_qs(request.user).order_by("category", "name")
    if category := request.query_params.get("category"):
        qs = qs.filter(category=category)
    if tool_type := request.query_params.get("tool_type"):
        qs = qs.filter(tool_type=tool_type)
    return Response(ToolDefinitionSerializer(qs, many=True).data)


@api_view(["GET"])
@authenticated_required
def tool_definition_detail(request, pk):
    """GET /api/tools/<id>/ — full detail including Grok schema."""
    try:
        tool = _visible_tools_qs(request.user).get(pk=pk)
    except ToolDefinition.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(ToolDefinitionSerializer(tool).data)


# ── ToolCall ──────────────────────────────────────────────────────────────────

@api_view(["GET"])
@authenticated_required
def tool_call_list(request):
    """GET /api/tools/calls/ — list tool calls with optional filters, scoped to this org."""
    qs = ToolCall.objects.filter(
        organization=request.user.organization
    ).select_related("tool", "job").order_by("-created_at")
    if job_id := request.query_params.get("job"):
        qs = qs.filter(job_id=job_id)
    if tool_name := request.query_params.get("tool"):
        qs = qs.filter(tool__name=tool_name)
    if status_val := request.query_params.get("status"):
        qs = qs.filter(status=status_val)
    return Response(ToolCallSerializer(qs, many=True).data)


@api_view(["GET"])
@authenticated_required
def tool_call_detail(request, pk):
    """GET /api/tools/calls/<id>/ — single call detail, scoped to this org."""
    try:
        tc = ToolCall.objects.filter(
            organization=request.user.organization
        ).select_related("tool", "job").get(pk=pk)
    except ToolCall.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(ToolCallSerializer(tc).data)


@api_view(["GET"])
@authenticated_required
def tool_call_output_download(request, pk):
    """
    GET /api/tools/calls/<id>/output/download/

    Download a tool call's output file (e.g. from write_xlsx/export_file),
    org-scoped. ToolRunView/CustomToolTestView only ever return
    {tool_call_id, result} with a raw server filesystem path embedded in
    result["output_filename"] — never useful (or safe) to hand a browser
    directly. This resolves and streams it instead, mirroring
    chat/views.py's ChatAttachmentDownloadView but keyed off ToolCall
    ownership rather than a chat conversation.
    """
    try:
        tc = ToolCall.objects.select_related("tool").get(
            pk=pk, organization=request.user.organization,
        )
    except ToolCall.DoesNotExist:
        return Response({"error": "Tool call not found."}, status=status.HTTP_404_NOT_FOUND)

    result = tc.result or {}
    output_path = result.get("output_filename")
    if not output_path:
        return Response({"error": "This tool call has no output file."}, status=status.HTTP_404_NOT_FOUND)

    p = Path(output_path)
    if not p.exists():
        return Response({"error": "Output file no longer exists."}, status=status.HTTP_404_NOT_FOUND)

    resp = FileResponse(p.open("rb"), as_attachment=True, filename=p.name)
    return resp


@api_view(["POST"])
@authenticated_required
def tool_run(request):
    """
    POST /api/tools/run/
    Execute a single tool directly (bypasses the LLM loop).
    Routes through ToolService._dispatch_single() so all three tool types
    (builtin, webhook, prompt_transform) work correctly.
    """
    tool_name = request.data.get("tool_name", "").strip()
    arguments = request.data.get("arguments", {})

    if not tool_name:
        return Response(
            {"error": "tool_name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        tool_def = _visible_tools_qs(request.user).select_related("user_config").get(
            name=tool_name
        )
    except ToolDefinition.DoesNotExist:
        return Response(
            {"error": f"Tool '{tool_name}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not tool_def.is_safe:
        return Response(
            {"error": f"Tool '{tool_name}' is marked unsafe and cannot be run directly."},
            status=status.HTTP_403_FORBIDDEN,
        )

    from datetime import datetime, timezone as tz
    from tools.services import _resolve_handler, _call_webhook, _call_prompt_transform

    started_at = datetime.now(tz.utc)
    try:
        tool_type = tool_def.tool_type

        if tool_type == "builtin":
            handler = _resolve_handler(tool_def.handler)
            result  = handler(**arguments)

        elif tool_type == "webhook":
            result = _call_webhook(tool_def.user_config, arguments)

        elif tool_type == "prompt_transform":
            result = _call_prompt_transform(tool_def.user_config, arguments)

        else:
            result = {"ok": False, "error": f"Unknown tool_type '{tool_type}'."}

        tc_status = "success" if result.get("ok", True) else "error"
        error_msg = result.get("error", "")

    except Exception as exc:
        result    = {"ok": False, "error": str(exc)}
        tc_status = "error"
        error_msg = str(exc)

    finished_at = datetime.now(tz.utc)
    tc = ToolCall.objects.create(
        tool=tool_def, arguments=arguments, result=result,
        status=tc_status, error_message=error_msg,
        started_at=started_at, finished_at=finished_at,
        organization=tool_def.organization or request.user.organization,
    )
    return Response({"tool_call_id": tc.pk, "result": result})


# ─────────────────────────────────────────────────────────────────────────────
# User-defined tools CRUD
# ─────────────────────────────────────────────────────────────────────────────

def _get_tool_for_view(pk, user):
    """Any org member may view."""
    try:
        return ToolDefinition.objects.select_related("user_config").get(
            pk=pk, organization=user.organization,
        )
    except ToolDefinition.DoesNotExist:
        return None


def _get_tool_for_edit(pk, user):
    """Only the creator or an org-admin may edit/delete."""
    tool = _get_tool_for_view(pk, user)
    if tool and (tool.created_by_id == user.id or user.role == "admin"):
        return tool
    return None


@api_view(["GET", "POST"])
@authenticated_required
def custom_tool_list_create(request):
    """
    GET  /api/tools/custom/   — list custom tools shared within the current org
    POST /api/tools/custom/   — create a new user-defined tool
    """
    if request.method == "GET":
        tools = (
            ToolDefinition.objects
            .filter(organization=request.user.organization)
            .select_related("user_config")
            .order_by("-created_at")
        )
        return Response(CustomToolDetailSerializer(tools, many=True).data)

    serializer = CustomToolCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    vd = serializer.validated_data

    # ── Create ToolDefinition ─────────────────────────────────────────────
    tool_type = vd["tool_type"]
    tool = ToolDefinition.objects.create(
        name              = vd["name"],
        display_name      = vd["display_name"],
        description       = vd["description"],
        category          = vd.get("category", "utility"),
        tool_type         = tool_type,
        parameters_schema = vd.get("parameters_schema", {}),
        # webhook tools are unsafe by default — they POST to external systems
        is_safe           = vd.get("is_safe", tool_type != "webhook"),
        handler           = "",   # not used for non-builtin tools
        enabled           = True,
        created_by        = request.user,
        organization      = request.user.organization,
    )

    # ── Create UserToolConfig ─────────────────────────────────────────────
    UserToolConfig.objects.create(
        tool                    = tool,
        webhook_url             = vd.get("webhook_url", ""),
        webhook_method          = vd.get("webhook_method", "POST"),
        webhook_headers         = vd.get("webhook_headers", {}),
        webhook_timeout_seconds = vd.get("webhook_timeout_seconds", 30),
        system_prompt           = vd.get("system_prompt", ""),
        output_schema           = vd.get("output_schema"),
    )

    return Response(
        CustomToolDetailSerializer(tool).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "PATCH", "DELETE"])
@authenticated_required
def custom_tool_detail(request, pk):
    """
    GET    /api/tools/custom/<id>/   — retrieve
    PATCH  /api/tools/custom/<id>/   — partial update
    DELETE /api/tools/custom/<id>/   — soft-delete (sets enabled=False)
    """
    if request.method == "GET":
        tool = _get_tool_for_view(pk, request.user)
        if not tool:
            return Response({"error": "Tool not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(CustomToolDetailSerializer(tool).data)

    if request.method == "PATCH":
        tool = _get_tool_for_edit(pk, request.user)
        if not tool:
            return Response({"error": "Tool not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = CustomToolUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data

        # ── Update ToolDefinition fields ──────────────────────────────────────
        tool_fields = ["display_name", "description", "category",
                       "parameters_schema", "is_safe", "enabled"]
        changed = False
        for field in tool_fields:
            if field in vd:
                setattr(tool, field, vd[field])
                changed = True
        if changed:
            tool.save()

        # ── Update UserToolConfig fields ──────────────────────────────────────
        config_fields = [
            "webhook_url", "webhook_method", "webhook_headers",
            "webhook_timeout_seconds", "system_prompt", "output_schema",
        ]
        config_data = {f: vd[f] for f in config_fields if f in vd}
        if config_data:
            cfg, _ = UserToolConfig.objects.get_or_create(tool=tool)
            for field, value in config_data.items():
                setattr(cfg, field, value)
            cfg.save()

        tool.refresh_from_db()
        return Response(CustomToolDetailSerializer(tool).data)

    # DELETE
    tool = _get_tool_for_edit(pk, request.user)
    if not tool:
        return Response({"error": "Tool not found."}, status=status.HTTP_404_NOT_FOUND)
    # Soft delete — keeps the audit trail of any ToolCall records
    tool.enabled = False
    tool.save(update_fields=["enabled"])
    return Response(
        {"detail": f"Tool '{tool.name}' has been disabled."},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@authenticated_required
def custom_tool_test(request, pk):
    """
    POST /api/tools/custom/<id>/test/

    Run the tool against a provided payload without going through the LLM
    tool-calling loop. Returns the raw handler/webhook/prompt result so
    the user can verify it works before enabling it for Grok to call.

    Request body:
        { "arguments": { ... } }

    The tool's is_safe flag is NOT checked here — the user is explicitly
    testing their own tool.
    """
    try:
        tool = ToolDefinition.objects.select_related("user_config").get(
            pk=pk, organization=request.user.organization,
        )
    except ToolDefinition.DoesNotExist:
        return Response(
            {"error": "Tool not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    arguments = request.data.get("arguments", {})
    if not isinstance(arguments, dict):
        return Response(
            {"error": "arguments must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from datetime import datetime, timezone as tz
    from tools.services import _resolve_handler, _call_webhook, _call_prompt_transform

    started_at = datetime.now(tz.utc)
    try:
        tool_type = tool.tool_type

        if tool_type == "builtin":
            result = {"ok": False, "error": "Built-in tools cannot be tested via this endpoint."}

        elif tool_type == "webhook":
            result = _call_webhook(tool.user_config, arguments)

        elif tool_type == "prompt_transform":
            result = _call_prompt_transform(tool.user_config, arguments)

        else:
            result = {"ok": False, "error": f"Unknown tool_type '{tool_type}'."}

    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    finished_at = datetime.now(tz.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    # Record the test call — job=None, visible in the audit log
    tc = ToolCall.objects.create(
        tool=tool, arguments=arguments, result=result,
        status="success" if result.get("ok") else "error",
        error_message=result.get("error", ""),
        started_at=started_at, finished_at=finished_at,
        organization=tool.organization or request.user.organization,
    )

    return Response({
        "tool_call_id": tc.pk,
        "duration_ms":  duration_ms,
        "result":       result,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Contract endpoints (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@authenticated_required
def tools_convert(request):
    """
    POST /api/tools/convert
    Convert a file to a target format and return a download URL.
    """
    from django.core.files.base import ContentFile
    from tools.handlers import convert_text_to_format

    uploaded      = request.FILES.get("file")
    target_format = (request.data.get("targetFormat") or "xlsx").lower()

    if not uploaded:
        return Response(
            {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    filename = uploaded.name
    out_name = Path(filename).stem + f".{target_format}"

    try:
        from uploads.services import UploadService
        from uploads.models import UploadBatch, UploadedFile

        batch  = UploadBatch.objects.create(label=f"convert-{filename}", uploaded_by=request.user)
        record = UploadService.ingest_file(batch, uploaded)
        text   = record.extracted_text or ""

        content, mime = convert_text_to_format(text, target_format)

        out_record = UploadedFile(
            batch=batch, original_filename=out_name,
            file_size_bytes=len(content),
            mime_type=mime or "application/octet-stream",
            extension=target_format, parse_status="parsed",
        )
        out_record.file.save(out_name, ContentFile(content), save=True)
        out_record.save()

        expires_at   = (timezone.now() + timedelta(hours=24)).isoformat()
        download_url = request.build_absolute_uri(f"/api/files/{out_record.pk}/download/")

    except Exception as exc:
        logger.exception("tools_convert: %s", exc)
        return Response(
            {"error": {"code": "server_error", "message": str(exc), "details": None}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({"downloadUrl": download_url, "fileName": out_name, "expiresAt": expires_at})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@authenticated_required
def tools_clean(request):
    """
    POST /api/tools/clean
    Clean and process data in an uploaded file.
    """
    import json as _json
    from io import BytesIO
    from django.core.files.base import ContentFile

    uploaded = request.FILES.get("file")
    ops_raw  = request.data.get("operations", "[]")

    if not uploaded:
        return Response(
            {"error": {"code": "bad_request", "message": "file is required.", "details": None}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        operations = _json.loads(ops_raw) if isinstance(ops_raw, str) else (ops_raw or [])
    except Exception:
        operations = []

    filename = uploaded.name
    out_name = Path(filename).stem + "_cleaned.xlsx"

    try:
        import openpyxl
        from uploads.services import UploadService
        from uploads.models import UploadBatch, UploadedFile

        batch  = UploadBatch.objects.create(label=f"clean-{filename}", uploaded_by=request.user)
        record = UploadService.ingest_file(batch, uploaded)
        text   = record.extracted_text or ""
        lines  = [l for l in text.splitlines() if l.strip()]
        rows_original = len(lines)

        if "trim" in operations:
            lines = [l.strip() for l in lines]
        if "deduplicate" in operations:
            seen, deduped = set(), []
            for l in lines:
                if l not in seen:
                    seen.add(l); deduped.append(l)
            lines = deduped
        if "remove_empty" in operations:
            lines = [l for l in lines if l.strip()]
        rows_cleaned = len(lines)

        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Cleaned"
        for i, line in enumerate(lines, 1):
            parts = line.split("\t") if "\t" in line else line.split(",")
            for j, part in enumerate(parts, 1):
                ws.cell(row=i, column=j, value=part.strip())
        buf = BytesIO(); wb.save(buf); content = buf.getvalue()

        out_record = UploadedFile(
            batch=batch, original_filename=out_name,
            file_size_bytes=len(content),
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            extension="xlsx", parse_status="parsed",
        )
        out_record.file.save(out_name, ContentFile(content), save=True)
        out_record.save()

        expires_at   = (timezone.now() + timedelta(hours=24)).isoformat()
        download_url = request.build_absolute_uri(f"/api/files/{out_record.pk}/download/")

    except Exception as exc:
        logger.exception("tools_clean: %s", exc)
        return Response(
            {"error": {"code": "server_error", "message": str(exc), "details": None}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        "downloadUrl": download_url,
        "fileName":    out_name,
        "stats": {
            "rowsProcessed": rows_original,
            "rowsCleaned":   rows_cleaned,
            "errorsFound":   0,
        },
    })


@api_view(["POST"])
@authenticated_required
def tools_validate(request):
    """
    POST /api/tools/validate
    Validate a file's data against a named schema.
    """
    file_id   = request.data.get("fileId")
    schema_id = (request.data.get("schemaId") or "default").lower()

    if not file_id:
        return Response(
            {"error": {"code": "bad_request", "message": "fileId is required.", "details": None}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from uploads.models import UploadedFile
        uf = UploadedFile.objects.get(pk=int(file_id))
    except (UploadedFile.DoesNotExist, ValueError, TypeError):
        return Response(
            {"error": {"code": "not_found", "message": "File not found.", "details": None}},
            status=status.HTTP_404_NOT_FOUND,
        )

    validation_errors = []
    text  = uf.extracted_text or ""
    lines = [l for l in text.splitlines() if l.strip()]

    if not lines:
        validation_errors.append({
            "row": 0, "column": "file",
            "message": "File appears to be empty or could not be parsed.",
        })

    if schema_id == "ura_receipt":
        for i, line in enumerate(lines, 1):
            if "FISCAL RECEIPT" not in line.upper() and "CU INVOICE" not in line.upper():
                continue
            if not any(c.isdigit() for c in line):
                validation_errors.append({
                    "row": i, "column": "CU Invoice Number",
                    "message": "Missing numeric invoice number.",
                })

    elif schema_id == "safaricom_bill":
        if lines:
            header_line = lines[0].upper()
            for h in ["NAME", "REFERENCE", "INVOICE", "AMOUNT"]:
                if h not in header_line:
                    validation_errors.append({
                        "row": 1, "column": h.title(),
                        "message": f"Expected column '{h.title()}' not found in header row.",
                    })

    return Response({"valid": len(validation_errors) == 0, "errors": validation_errors})
