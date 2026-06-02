# analytics/models.py
"""
Analytics app — no dedicated DB models needed.
All metrics are computed on-the-fly from existing models:
  - uploads.UploadBatch / UploadedFile  → invoice processing counts
  - ai_engine.AIAnalysisJob             → automation + accuracy metrics
  - chat.ChatConversation               → activity feed

A Report model is included here to support the /api/reports/ contract.
"""
from django.db import models


class Report(models.Model):
    """
    A generated report file (PDF, XLSX, CSV).
    Created by POST /api/reports/generate and downloaded via GET /api/reports/<id>/download.
    """

    REPORT_TYPE_CHOICES = [
        ("reconciliation", "Reconciliation"),
        ("billing",        "Billing"),
        ("analytics",      "Analytics"),
        ("custom",         "Custom"),
    ]

    STATUS_CHOICES = [
        ("ready",      "Ready"),
        ("generating", "Generating"),
        ("error",      "Error"),
    ]

    FORMAT_CHOICES = [
        ("pdf",  "PDF"),
        ("xlsx", "Excel"),
        ("csv",  "CSV"),
    ]

    name        = models.CharField(max_length=255)
    report_type = models.CharField(max_length=20, choices=REPORT_TYPE_CHOICES, default="custom")
    status      = models.CharField(max_length=15, choices=STATUS_CHOICES, default="generating")
    format      = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="pdf")

    # The generated file
    file      = models.FileField(upload_to="reports/%Y/%m/", null=True, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)

    # Parameters used to generate this report (stored for re-generation)
    parameters = models.JSONField(default=dict, blank=True)

    requested_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="reports",
    )

    error_message = models.TextField(blank=True)

    generated_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Report"
        verbose_name_plural = "Reports"

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"
