# analytics/report_urls.py — mounted at /api/reports/
from django.urls import path
from .views import ReportListView, ReportGenerateView, ReportDownloadView

urlpatterns = [
    # GET  /api/reports/           list reports
    path("",                ReportListView.as_view(),    name="report-list"),

    # POST /api/reports/generate   queue a new report
    path("generate/",       ReportGenerateView.as_view(), name="report-generate"),

    # GET  /api/reports/<id>/download
    path("<int:pk>/download/", ReportDownloadView.as_view(), name="report-download"),
]
