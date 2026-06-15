# uploads/urls.py
from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import permissions

from .views import (
    UploadBatchListCreateView,
    UploadBatchDetailView,
)
from .models import UploadBatch
from .serializers import UploadBatchSerializer


class UploadBatchSummaryView(APIView):
    """
    GET /api/uploads/batches/<id>/summary/

    Lightweight summary used by the chat offline fallback and status intent.
    Returns counts and per-file parse status without the full file payload.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            batch = UploadBatch.objects.filter(
                uploaded_by=request.user
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
                    "id":           f.pk,
                    "name":         f.original_filename,
                    "parse_status": f.parse_status,
                    "detected_type": f.detected_type,
                    "parse_error":  f.parse_error or None,
                }
                for f in files.order_by("uploaded_at")
            ],
        })


urlpatterns = [
    # GET  /api/uploads/batches/        list user's batches
    # POST /api/uploads/batches/        create a new batch
    path("batches/",                UploadBatchListCreateView.as_view(), name="upload-batch-list"),

    # GET    /api/uploads/batches/<id>/ batch detail + files
    # DELETE /api/uploads/batches/<id>/ delete batch
    path("batches/<int:pk>/",       UploadBatchDetailView.as_view(),     name="upload-batch-detail"),

    # GET /api/uploads/batches/<id>/summary/
    path("batches/<int:pk>/summary/", UploadBatchSummaryView.as_view(),  name="upload-batch-summary"),
]