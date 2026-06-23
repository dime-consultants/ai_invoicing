# ai_engine/tasks.py
"""
Celery tasks for ai_engine.

Keeping this as a thin wrapper around AIEngineService.dispatch() means
the actual job-running logic (tool loop, insight persistence, status
transitions) lives in exactly one place — ai_engine/services.py — and
this file is only responsible for *how* that logic gets invoked
(synchronously inline vs. asynchronously via Celery).
"""
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=0,
    soft_time_limit=60 * 25,   # matches CELERY_TASK_SOFT_TIME_LIMIT
    time_limit=60 * 30,        # matches CELERY_TASK_TIME_LIMIT
)
def run_ai_job_task(self, job_id: int):
    """
    Run a queued AIAnalysisJob in a Celery worker process instead of
    inline in an HTTP request thread.

    On a soft time limit (5 min before the hard kill), mark the job as
    errored with a clear message rather than letting Celery silently
    kill the worker mid-extraction and leave the job stuck in 'running'
    forever.
    """
    from .services import AIEngineService
    from .models import AIAnalysisJob

    try:
        AIEngineService.dispatch(job_id)
    except SoftTimeLimitExceeded:
        logger.error("Job %s exceeded soft time limit — marking as error", job_id)
        try:
            from datetime import datetime, timezone as tz
            job = AIAnalysisJob.objects.get(pk=job_id)
            job.status = "error"
            job.error_message = (
                "Processing took too long and was stopped automatically. "
                "Try splitting the file into smaller batches."
            )
            job.finished_at = datetime.now(tz.utc)
            job.save(update_fields=["status", "error_message", "finished_at"])
        except Exception:
            logger.exception("Could not mark job %s as timed-out", job_id)
        raise
    except Exception as exc:
        # AIEngineService.dispatch already catches and records failures on
        # the job itself, but log here too so Celery's own monitoring
        # (e.g. flower, retries) sees the failure.
        logger.exception("run_ai_job_task failed for job %s: %s", job_id, exc)
        raise