"""
Safe Celery dispatch.

`task.delay(...)` opens a broker connection at call time. If Redis is
unreachable, kombu raises OperationalError *inside the web request* — after the
caller has already committed its own state (a ChatMessage, an AIAnalysisJob
reset to "queued" with its previous insights deleted). The request then 500s
and that state is orphaned with nothing queued to advance it.

dispatch() converts that into a return value so callers can degrade
deliberately instead of crashing:

    from config.dispatch import dispatch

    if not dispatch(run_ai_job_task, job.id):
        job.status = "error"
        job.error_message = "Could not queue the job — the task broker is unavailable."
        job.save(update_fields=["status", "error_message"])

Note this only covers *dispatch* failure. Once a task is queued, delivery is
Celery's problem (see CELERY_TASK_ACKS_LATE in settings).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def dispatch(task, *args, **kwargs) -> Any | None:
    """
    Queue `task` with Celery. Returns the AsyncResult, or None if the broker
    could not be reached. Never raises.
    """
    try:
        # Celery's `delay` accepts positional and keyword args, but many
        # callsites (and our tasks) prefer keyword-only signatures. To avoid
        # accidental positional mis-mapping, prefer calling `apply_async`
        # with an explicit `args` tuple and `kwargs` dict so callers can pass
        # either form to `dispatch` and have semantics preserved.
        if args and kwargs:
            return task.apply_async(args=args, kwargs=kwargs)
        if args:
            return task.apply_async(args=args)
        return task.apply_async(kwargs=kwargs)
    except Exception as exc:
        logger.exception(
            "Could not queue task %s: %s",
            getattr(task, "name", task), exc,
        )
        return None
