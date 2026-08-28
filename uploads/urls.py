from django.urls import path

from .views import (
    upload_batch_list_create,
    upload_batch_detail,
    upload_batch_summary,
    file_list,
    file_upload,
    file_download,
    file_delete,
    file_reextract,
)


urlpatterns = [
    # GET  /api/uploads/batches/        list user's batches
    # POST /api/uploads/batches/        create a new batch
    path("batches/",                upload_batch_list_create, name="upload-batch-list"),

    # GET    /api/uploads/batches/<id>/ batch detail + files
    # DELETE /api/uploads/batches/<id>/ delete batch
    path("batches/<int:pk>/",       upload_batch_detail,     name="upload-batch-detail"),

    # GET /api/uploads/batches/<id>/summary/
    path("batches/<int:pk>/summary/", upload_batch_summary,  name="upload-batch-summary"),

    # File-level endpoints (also mountable under /api/files/)
    path("files/",                  file_list,               name="file-list"),
    path("files/upload/",           file_upload,             name="file-upload"),
    path("files/<int:pk>/download/", file_download,          name="file-download"),
    path("files/<int:pk>/",         file_delete,             name="file-delete"),
    path("files/<int:pk>/reextract/", file_reextract,        name="file-reextract"),
]
