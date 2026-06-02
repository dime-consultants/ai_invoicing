# ai_engine/signals.py
"""
Django signals for the ai_engine app.
Fires WebSocket notifications when an AIAnalysisJob status changes.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="ai_engine.AIAnalysisJob")
def on_job_saved(sender, instance, created, **kwargs):
    """
    Push a WS notification when a job transitions to done or error.
    Also pushes a progress update when it moves to running.
    """
    if created:
        return

    status = instance.status
    if status not in ("running", "done", "error"):
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
            title=f"{task_label} — {status}",
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
