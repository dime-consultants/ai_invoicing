# analytics/tasks.py
"""
Celery task for report generation. Thin wrapper, mirroring
ai_engine/tasks.py::run_ai_job_task — the real work lives in
ReportBuildService, this file is only responsible for how it's invoked and
for making sure a crashed/killed worker can never leave a Report stuck at
status="generating" forever.
"""
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    soft_time_limit=60 * 5,
    time_limit=60 * 6,
)
def generate_report_task(self, report_id: int):
    from .models import Report
    from .services import ReportBuildService

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        logger.warning("generate_report_task: Report %s no longer exists", report_id)
        return

    try:
        ReportBuildService.build(report)
    except SoftTimeLimitExceeded:
        logger.error("generate_report_task: report %s exceeded soft time limit", report_id)
        report.status = "error"
        report.error_message = "Report generation took too long and was stopped automatically."
        report.save(update_fields=["status", "error_message"])
        raise
    except Exception as exc:
        logger.exception("generate_report_task failed for report %s: %s", report_id, exc)
        report.status = "error"
        report.error_message = str(exc)[:2000]
        report.save(update_fields=["status", "error_message"])
        raise
