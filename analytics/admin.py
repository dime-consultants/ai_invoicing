from django.contrib import admin
from .models import Report

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display  = ["name", "report_type", "status", "format", "requested_by", "created_at"]
    list_filter   = ["report_type", "status", "format"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "updated_at", "generated_at"]
