# uploads/files_urls.py
# Mounted at /api/files/ — matches the frontend API contract
from django.urls import path
from .views import (
    FileListView,
    FileUploadView,
    FileDownloadView,
    FileDeleteView,
)

urlpatterns = [
    # GET  /api/files/              list files (paginated, filterable)
    path("",                FileListView.as_view(),    name="file-list"),

    # POST /api/files/upload        upload a file
    path("upload/",         FileUploadView.as_view(),  name="file-upload"),

    # GET  /api/files/<id>/download stream file
    path("<int:pk>/download/", FileDownloadView.as_view(), name="file-download"),

    # DELETE /api/files/<id>/       delete file
    path("<int:pk>/",       FileDeleteView.as_view(),  name="file-delete"),
]
