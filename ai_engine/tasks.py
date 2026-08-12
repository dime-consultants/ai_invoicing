# ai_engine/tasks.py
"""
Celery tasks for ai_engine.

Keeping this as a thin wrapper around AIEngineService.dispatch() means
the actual job-running logic (tool loop, insight persistence, status
transitions) lives in exactly one place — ai_engine/services.py — and
this file is only responsible for *how* that logic gets invoked
(synchronously inline vs. asynchronously via Celery).

Reliability
-----------
acks_late (explicit here, also the global default) means a worker crash
mid-job gets this task redelivered rather than losing it. That's safe to
retry blindly because AIEngineService.dispatch() is already idempotent — it
no-ops if job.status != "queued" (services.py), so a redelivered or manually
retried call for a job that already finished/failed is a cheap skip, not a
duplicate run. Transient errors (Grok rate limits/connection errors) get a
bounded retry with backoff; anything else still surfaces on the job row via
AIEngineService's own except-block (job.status="error").
"""
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
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
        from tools.services import is_transient_llm_error
        if is_transient_llm_error(exc) and self.request.retries < self.max_retries:
            # AIEngineService._run_job() already wrote status="error" for
            # this attempt before re-raising. Reset back to "queued" so
            # dispatch() (which no-ops on any status other than "queued")
            # actually re-runs the job on retry instead of silently skipping it.
            try:
                job = AIAnalysisJob.objects.get(pk=job_id)
                if job.status in ("running", "error"):
                    job.status = "queued"
                    job.save(update_fields=["status"])
            except Exception:
                logger.exception("Could not reset job %s to queued before retry", job_id)
            raise self.retry(exc=exc, countdown=15 * (self.request.retries + 1))
        raise