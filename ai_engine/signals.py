# ai_engine/signals.py
"""
Django signals for the ai_engine app.
Fires WebSocket notifications when an AIAnalysisJob status changes, and
when a large async job reports incremental progress (e.g. "Processed
450/1000 pages") so the user isn't staring at a silent spinner for a
long-running Celery-backed job.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="ai_engine.AIAnalysisJob")
def on_job_saved(sender, instance, created, **kwargs):
    """
    Push a WS notification when a job transitions to done or error.
    Also pushes a progress update when it moves to running, and again
    whenever progress_note changes while still running (e.g. large
    multi-page PDF jobs processed via Celery).
    """
    if created:
        return

    status = instance.status
    progress_note = getattr(instance, "progress_note", "") or ""

    # Only notify on a real status transition, or a progress update while
    # running. Anything else (e.g. a save that only touches raw_response)
    # is noise we don't want to push to the client every time.
    update_fields = kwargs.get("update_fields") or set()
    is_progress_update = status == "running" and "progress_note" in update_fields

    if status not in ("running", "done", "error") and not is_progress_update:
        return

    user = instance.requested_by
    if not user:
        return

    try:
        from chat.notify import notify_user, notify_job_update

        task_label = instance.get_task_type_display()

        notify_job_update(
            user.pk,
            job_id=instance.pk,
            status=status,
            title=(
                f"{task_label} — {progress_note}"
                if is_progress_update and progress_note
                else f"{task_label} — {status}"
            ),
        )

        if status == "done":
            insight_count = instance.insights.count()
            message = (
                f"AI job '{task_label}' completed. "
                f"{insight_count} insight{'s' if insight_count != 1 else ''} generated."
            )
            notify_user(user.pk, title="AI Job Complete", message=message)

        elif status == "error":
            notify_user(
                user.pk,
                title="AI Job Failed",
                message=f"'{task_label}' failed: {instance.error_message[:120]}",
            )

    except Exception:
        pass