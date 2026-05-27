# uploads/views.py
import mimetypes
import os

from django.http import FileResponse
from rest_framework import generics, permissions, status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UploadBatch, UploadedFile
from .serializers import (
    UploadBatchSerializer,
    UploadBatchListSerializer,
    UploadedFileSerializer,
    FileUploadSerializer,
)


# ── Permissions ───────────────────────────────────────────────────────────────

class CanUpload(permissions.BasePermission):
    """Admin and finance users may upload files."""
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and getattr(request.user, "role", None) in ("admin", "finance")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Batches
# ─────────────────────────────────────────────────────────────────────────────

class UploadBatchListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/uploads/batches/   — list all batches for the current user
    POST /api/uploads/batches/   — create a new batch
    """
    permission_classes = [CanUpload]

    def get_serializer_class(self):
        return UploadBatchSerializer if self.request.method == "POST" else UploadBatchListSerializer

    def get_queryset(self):
        return UploadBatch.objects.filter(
            uploaded_by=self.request.user
        ).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        label       = (request.data.get("label") or "Untitled Batch").strip()
        description = request.data.get("description", "")
        batch = UploadBatch.objects.create(
            label=label,
            description=description,
            uploaded_by=request.user,
        )
        return Response(UploadBatchSerializer(batch).data, status=status.HTTP_201_CREATED)


class UploadBatchDetailView(generics.RetrieveDestroyAPIView):
    """
    GET    /api/uploads/batches/<id>/   — batch detail with files
    DELETE /api/uploads/batches/<id>/   — delete batch and all its files
    """
    permission_classes = [CanUpload]
    serializer_class   = UploadBatchSerializer

    def get_queryset(self):
        return UploadBatch.objects.filter(uploaded_by=self.request.user)


# ─────────────────────────────────────────────────────────────────────────────
# Files — contract-compatible endpoints
# ─────────────────────────────────────────────────────────────────────────────

class FileListView(generics.ListAPIView):
    """
    GET /api/files/
    List uploaded files with pagination and optional filters.

    Query params:
        page, limit, type, search
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class   = UploadedFileSerializer

    def get_queryset(self):
        qs = UploadedFile.objects.filter(
            batch__uploaded_by=self.request.user
        ).select_related("batch").order_by("-uploaded_at")

        file_type = self.request.query_params.get("type")
        search    = self.request.query_params.get("search")

        if file_type:
            qs = qs.filter(extension=file_type.lower())
        if search:
            qs = qs.filter(original_filename__icontains=search)

        return qs

    def list(self, request, *args, **kwargs):
        qs    = self.get_queryset()
        page  = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 20))

        total       = qs.count()
        total_pages = max(1, (total + limit - 1) // limit)
        offset      = (page - 1) * limit
        page_qs     = qs[offset: offset + limit]

        serializer = self.get_serializer(page_qs, many=True)
        return Response({
            "files":      serializer.data,
            "total":      total,
            "page":       page,
            "totalPages": total_pages,
        })


class FileUploadView(APIView):
    """
    POST /api/files/upload
    Upload a single file. Creates a new batch automatically.
    """
    permission_classes = [CanUpload]
    parser_classes     = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded = serializer.validated_data["file"]
        file_type = serializer.validated_data.get("type", "")

        ext      = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
        mime, _  = mimetypes.guess_type(uploaded.name)
        mime     = mime or "application/octet-stream"

        # Auto-create a batch for this single upload
        batch = UploadBatch.objects.create(
            label=uploaded.name,
            uploaded_by=request.user,
        )

        uf = UploadedFile.objects.create(
            batch=batch,
            file=uploaded,
            original_filename=uploaded.name,
            file_size_bytes=uploaded.size,
            mime_type=mime,
            extension=ext,
            detected_type=file_type,
        )

        # Update batch counter
        batch.file_count = 1
        batch.save(update_fields=["file_count"])

        return Response({
            "id":         uf.pk,
            "name":       uf.original_filename,
            "type":       uf.mime_type,
            "size":       uf.file_size_bytes,
            "uploadedAt": uf.uploaded_at,
        }, status=status.HTTP_200_OK)


class FileDownloadView(APIView):
    """
    GET /api/files/<id>/download
    Stream the file to the client.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            uf = UploadedFile.objects.select_related("batch").get(pk=pk)
        except UploadedFile.DoesNotExist:
            return Response({"error": {"code": "not_found", "message": "File not found.", "details": None}},
                            status=status.HTTP_404_NOT_FOUND)

        if uf.batch.uploaded_by != request.user and not request.user.is_admin:
            return Response({"error": {"code": "forbidden", "message": "Access denied.", "details": None}},
                            status=status.HTTP_403_FORBIDDEN)

        if not uf.file:
            return Response({"error": {"code": "not_found", "message": "File not available.", "details": None}},
                            status=status.HTTP_404_NOT_FOUND)

        response = FileResponse(
            uf.file.open("rb"),
            content_type=uf.mime_type or "application/octet-stream",
            as_attachment=True,
        )
        response["Content-Disposition"] = f'attachment; filename="{uf.original_filename}"'
        return response


class FileDeleteView(APIView):
    """
    DELETE /api/files/<id>/
    Delete a file record and its stored file.
    """
    permission_classes = [CanUpload]

    def delete(self, request, pk):
        try:
            uf = UploadedFile.objects.select_related("batch").get(pk=pk)
        except UploadedFile.DoesNotExist:
            return Response({"error": {"code": "not_found", "message": "File not found.", "details": None}},
                            status=status.HTTP_404_NOT_FOUND)

        if uf.batch.uploaded_by != request.user and not request.user.is_admin:
            return Response({"error": {"code": "forbidden", "message": "Access denied.", "details": None}},
                            status=status.HTTP_403_FORBIDDEN)

        # Delete the physical file
        if uf.file:
            try:
                storage = uf.file.storage
                storage.delete(uf.file.name)
            except Exception:
                pass

        uf.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
