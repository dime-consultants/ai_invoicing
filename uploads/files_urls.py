# uploads/files_urls.py/duplicated with uploads/urls.py for some endpoints
# Mounted at /api/files/ — matches the frontend API contract
from django.urls import path
from .views import (
    file_list,
    file_upload,
    file_download,
    file_delete,
)

urlpatterns = [
    # GET  /api/files/              list files (paginated, filterable)
    path("",                file_list,     name="file-list"),

    # POST /api/files/upload        upload a file
    path("upload/",         file_upload,   name="file-upload"),

    # GET  /api/files/<id>/download stream file
    path("<int:pk>/download/", file_download, name="file-download"),

    # DELETE /api/files/<id>/       delete file
    path("<int:pk>/",       file_delete,   name="file-delete"),
]
