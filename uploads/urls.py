# uploads/urls.py
from django.urls import path
from .views import (
    UploadBatchListCreateView,
    UploadBatchDetailView,
    FileListView,
    FileUploadView,
    FileDownloadView,
    FileDeleteView,
)

urlpatterns = [
    # ── Batches ───────────────────────────────────────────────────────────────
    # GET  /api/uploads/batches/        list user's batches
    # POST /api/uploads/batches/        create a new batch
    path("batches/",          UploadBatchListCreateView.as_view(), name="upload-batch-list"),

    # GET    /api/uploads/batches/<id>/ batch detail + files
    # DELETE /api/uploads/batches/<id>/ delete batch
    path("batches/<int:pk>/", UploadBatchDetailView.as_view(),     name="upload-batch-detail"),
]

# ── File management endpoints (contract: /api/files/) ─────────────────────────
# These are registered in config/urls.py under /api/files/ via files_urls.py
