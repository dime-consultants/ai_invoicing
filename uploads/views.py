from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from .models import UploadBatch, UploadedFile
from .serializers import (
    UploadBatchSerializer,
    UploadBatchListSerializer,
    UploadedFileSerializer,
    FileUploadSerializer,
)
from .services import UploadService
from users.decorators import authenticated_required, can_upload_or_run_jobs_required


def _user_owns_or_is_admin(request, uploaded_file: UploadedFile) -> bool:
    """Owner, or an admin — but only within the same organization. A cross-org
    admin gets no bypass; org membership is the outer boundary."""
    if uploaded_file.batch.organization_id != request.user.organization_id:
        return False
    if uploaded_file.batch.uploaded_by == request.user:
        return True
    return getattr(request.user, "role", None) == "admin"


# ─────────────────────────────────────────────────────────────────────────────
# Batches
# ─────────────────────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@can_upload_or_run_jobs_required
def upload_batch_list_create(request):
    """
    GET  /api/uploads/batches/   — list all batches for the current user
    POST /api/uploads/batches/   — create a new batch
    """
    if request.method == "GET":
        batches = UploadBatch.objects.filter(
            organization=request.user.organization
        ).order_by("-created_at")
        return Response(UploadBatchListSerializer(batches, many=True).data)

    label = (request.data.get("label") or "Untitled Batch").strip()
    description = request.data.get("description", "")
    batch = UploadService.create_batch(
        label=label,
        description=description,
        user=request.user,
    )
    return Response(UploadBatchSerializer(batch).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE"])
@can_upload_or_run_jobs_required
def upload_batch_detail(request, pk):
    """
    GET    /api/uploads/batches/<id>/   — batch detail with files
    DELETE /api/uploads/batches/<id>/   — delete batch and all its files
    """
    try:
        batch = UploadBatch.objects.filter(organization=request.user.organization).get(pk=pk)
    except UploadBatch.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(UploadBatchSerializer(batch).data)

    batch.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@authenticated_required
def upload_batch_summary(request, pk):
    """
    GET /api/uploads/batches/<id>/summary/

    Lightweight summary used by the chat offline fallback and status intent.
    Returns counts and per-file parse status without the full file payload.
    """
    try:
        batch = UploadBatch.objects.filter(
            organization=request.user.organization
        ).get(pk=pk)
    except UploadBatch.DoesNotExist:
        return Response(
            {"error": {"code": "not_found", "message": "Batch not found.", "details": None}},
            status=404,
        )

    files = batch.files.all()
    return Response({
        "id":              batch.pk,
        "label":           batch.label,
        "status":          batch.status,
        "file_count":      batch.file_count,
        "processed_count": batch.processed_count,
        "error_count":     batch.error_count,
        "files": [
            {
                "id":                  f.pk,
                "name":                f.original_filename,
                "parse_status":        f.parse_status,
                "detected_type":       f.detected_type,
                "parse_error":         f.parse_error or None,
                "page_count":          f.page_count,
                "extraction_deferred": f.extraction_deferred,
            }
            for f in files.order_by("uploaded_at")
        ],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

@api_view(["GET"])
@authenticated_required
def file_list(request):
    """
    GET /api/files/
    List uploaded files with pagination and optional filters.
    """
    qs = UploadedFile.objects.filter(
        batch__organization=request.user.organization
    ).select_related("batch").order_by("-uploaded_at")

    file_type = request.query_params.get("type")
    search    = request.query_params.get("search")

    if file_type:
        qs = qs.filter(extension=file_type.lower())
    if search:
        qs = qs.filter(original_filename__icontains=search)

    page  = int(request.query_params.get("page", 1))
    limit = int(request.query_params.get("limit", 20))

    total       = qs.count()
    total_pages = max(1, (total + limit - 1) // limit)
    offset      = (page - 1) * limit
    page_qs     = qs[offset: offset + limit]

    serializer = UploadedFileSerializer(page_qs, many=True)
    return Response({
        "files":      serializer.data,
        "total":      total,
        "page":       page,
        "totalPages": total_pages,
    })


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@can_upload_or_run_jobs_required
def file_upload(request):
    """
    POST /api/files/upload
    Upload a single file. Creates a new batch and runs (or queues) extraction.
    Large PDFs return immediately with status=pending / extractionDeferred=true.
    """
    serializer = FileUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    uploaded  = serializer.validated_data["file"]
    file_type = serializer.validated_data.get("type", "")

    batch = UploadService.create_batch(
        label=uploaded.name,
        user=request.user,
    )
    uf = UploadService.ingest_file(batch, uploaded)

    if file_type and not uf.detected_type:
        uf.detected_type = file_type
        uf.save(update_fields=["detected_type"])

    return Response({
        "id":                  uf.pk,
        "name":                uf.original_filename,
        "type":                uf.mime_type,
        "size":                uf.file_size_bytes,
        "status":              uf.parse_status,
        "pageCount":           uf.page_count,
        "extractionDeferred":  uf.extraction_deferred,
        "uploadedAt":          uf.uploaded_at,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@authenticated_required
def file_download(request, pk):
    """GET /api/files/<id>/download — stream the file."""
    try:
        uf = UploadedFile.objects.select_related("batch").get(pk=pk)
    except UploadedFile.DoesNotExist:
        return Response(
            {"error": {"code": "not_found", "message": "File not found.", "details": None}},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not _user_owns_or_is_admin(request, uf):
        return Response(
            {"error": {"code": "forbidden", "message": "Access denied.", "details": None}},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not uf.file:
        return Response(
            {"error": {"code": "not_found", "message": "File not available.", "details": None}},
            status=status.HTTP_404_NOT_FOUND,
        )

    response = FileResponse(
        uf.file.open("rb"),
        content_type=uf.mime_type or "application/octet-stream",
        as_attachment=True,
    )
    response["Content-Disposition"] = f'attachment; filename="{uf.original_filename}"'
    return response


@api_view(["DELETE"])
@can_upload_or_run_jobs_required
def file_delete(request, pk):
    """DELETE /api/files/<id>/ — delete a file record and its stored file."""
    try:
        uf = UploadedFile.objects.select_related("batch").get(pk=pk)
    except UploadedFile.DoesNotExist:
        return Response(
            {"error": {"code": "not_found", "message": "File not found.", "details": None}},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not _user_owns_or_is_admin(request, uf):
        return Response(
            {"error": {"code": "forbidden", "message": "Access denied.", "details": None}},
            status=status.HTTP_403_FORBIDDEN,
        )

    batch = uf.batch
    if uf.file:
        try:
            uf.file.storage.delete(uf.file.name)
        except Exception:
            pass

    uf.delete()

    try:
        UploadService._refresh_batch_counters(batch)
    except Exception:
        pass

    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
@can_upload_or_run_jobs_required
def file_reextract(request, pk):
    """
    POST /api/files/<id>/reextract/
    Force re-queue of text extraction (useful after parse_error or for large PDFs).
    """
    try:
        uf = UploadedFile.objects.select_related("batch").get(pk=pk)
    except UploadedFile.DoesNotExist:
        return Response(
            {"error": {"code": "not_found", "message": "File not found.", "details": None}},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not _user_owns_or_is_admin(request, uf):
        return Response(
            {"error": {"code": "forbidden", "message": "Access denied.", "details": None}},
            status=status.HTTP_403_FORBIDDEN,
        )

    from uploads.tasks import reextract_file_task
    from config.dispatch import dispatch
    if dispatch(reextract_file_task, uf.pk) is None:
        return Response(
            {"error": {"code": "broker_unavailable",
                       "message": "Could not queue re-extraction — the task "
                                  "broker is unavailable. Please retry.",
                       "details": None}},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({
        "id":     uf.pk,
        "status": "pending",
        "message": "Re-extraction queued.",
    })
