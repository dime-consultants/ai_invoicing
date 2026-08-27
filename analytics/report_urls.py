# analytics/report_urls.py — mounted at /api/reports/
from django.urls import path
from .views import report_list, report_generate, report_download

urlpatterns = [
    # GET  /api/reports/           list reports
    path("",                report_list,     name="report-list"),

    # POST /api/reports/generate   queue a new report
    path("generate/",       report_generate, name="report-generate"),

    # GET  /api/reports/<id>/download
    path("<int:pk>/download/", report_download, name="report-download"),
]
