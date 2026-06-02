# analytics/signals.py
"""
Django signals for the analytics app.
Fires WebSocket notifications when a Report becomes ready or errors.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="analytics.Report")
def on_report_saved(sender, instance, created, **kwargs):
    """Push a WS notification when a report transitions to ready or error."""
    if created:
        return

    status = instance.status
    if status not in ("ready", "error"):
        return

    user = instance.requested_by
    if not user:
        return

    try:
        from chat.notify import notify_user

        if status == "ready":
            notify_user(
                user.pk,
                title="Report Ready",
                message=f"Your report '{instance.name}' is ready to download.",
            )
        else:
            notify_user(
                user.pk,
                title="Report Failed",
                message=f"Report '{instance.name}' could not be generated: {instance.error_message[:120]}",
            )

    except Exception:
        pass
