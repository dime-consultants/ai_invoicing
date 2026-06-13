# tools/views.py
import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ToolDefinition, ToolCall
from .serializers import ToolDefinitionSerializer, ToolCallSerializer

logger = logging.getLogger(__name__)


# ── Permissions ───────────────────────────────────────────────────────────────

class IsAdminOrFinance(permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role in ("admin", "finance")
        )


# ── ToolDefinition ────────────────────────────────────────────────────────────

class ToolDefinitionListView(generics.ListAPIView):
    """GET /api/tools/ — list all enabled tools."""
    serializer_class   = ToolDefinitionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = ToolDefinition.objects.filter(enabled=True).order_by("category", "name")
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        return qs


class ToolDefinitionDetailView(generics.RetrieveAPIView):
    """GET /api/tools/<id>/ — full detail including Grok schema."""
    serializer_class   = ToolDefinitionSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset           = ToolDefinition.objects.filter(enabled=True)


# ── ToolCall ──────────────────────────────────────────────────────────────────

class ToolCallListView(generics.ListAPIView):
    """GET /api/tools/calls/ — list tool calls with optional filters."""
    serializer_class   = ToolCallSerializer
    permission_classes = [permissions.IsAuthenticated]

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
    """GET /api/tools/calls/<id>/ — single call detail."""
    serializer_class   = ToolCallSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset           = ToolCall.objects.select_related("tool", "job")


class ToolRunView(APIView):
    """
    POST /api/tools/run/
    Execute a single tool directly (bypasses the LLM loop).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tool_name = request.data.get("tool_name", "").strip()
        arguments = request.data.get("arguments", {})

        if not tool_name:
            return Response({"error": "tool_name is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tool_def = ToolDefinition.objects.get(name=tool_name, enabled=True)
        except ToolDefinition.DoesNotExist:
            return Response({"error": f"Tool '{tool_name}' not found."}, status=status.HTTP_404_NOT_FOUND)

        if not tool_def.is_safe:
            return Response({"error": f"Tool '{tool_name}' is marked unsafe."}, status=status.HTTP_403_FORBIDDEN)

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
            tool=tool_def, arguments=arguments, result=result,
            status=tc_status, error_message=error_msg,
            started_at=started_at, finished_at=finished_at,
        )
        return Response({"tool_call_id": tc.pk, "result": result})


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/tools/convert
# ─────────────────────────────────────────────────────────────────────────────

class ToolsConvertView(APIView):
    """
    POST /api/tools/convert
    Convert a file to a target format and return a download URL.

    multipart/form-data:
        file          File object
        targetFormat  xlsx | csv | json
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def post(self, request):
        import csv as _csv, json as _json, mimetypes
        from io import BytesIO, StringIO
        from pathlib import Path
        from django.core.files.base import ContentFile

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
            lines  = [l for l in text.splitlines() if l.strip()]

            if target_format == "xlsx":
                import openpyxl
                wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Converted"
                for i, line in enumerate(lines, 1):
                    parts = line.split("\t") if "\t" in line else line.split(",")
                    for j, part in enumerate(parts, 1):
                        ws.cell(row=i, column=j, value=part.strip())
                buf = BytesIO(); wb.save(buf); content = buf.getvalue()
            elif target_format == "csv":
                buf = StringIO()
                writer = _csv.writer(buf)
                for line in lines:
                    writer.writerow(line.split("\t") if "\t" in line else [line])
                content = buf.getvalue().encode("utf-8")
            elif target_format == "json":
                content = _json.dumps({"rows": lines}, indent=2).encode("utf-8")
            else:
                content = text.encode("utf-8")

            mime, _ = mimetypes.guess_type(out_name)
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
            logger.exception("ToolsConvertView: %s", exc)
            return Response(
                {"error": {"code": "server_error", "message": str(exc), "details": None}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"downloadUrl": download_url, "fileName": out_name, "expiresAt": expires_at})


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/tools/clean
# ─────────────────────────────────────────────────────────────────────────────

class ToolsCleanView(APIView):
    """
    POST /api/tools/clean
    Clean and process data in an uploaded file.

    multipart/form-data:
        file        File object
        operations  JSON string e.g. ["trim","deduplicate","remove_empty"]
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def post(self, request):
        import json as _json
        from io import BytesIO
        from pathlib import Path
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
            logger.exception("ToolsCleanView: %s", exc)
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


# ─────────────────────────────────────────────────────────────────────────────
# Contract: POST /api/tools/validate
# ─────────────────────────────────────────────────────────────────────────────

class ToolsValidateView(APIView):
    """
    POST /api/tools/validate
    Validate a file's data against a named schema.

    JSON body:
        { "fileId": "string", "schemaId": "string" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
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
