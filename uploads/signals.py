# uploads/signals.py
"""
Django signals for the uploads app.
Fires WebSocket notifications when a file's parse status changes.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="uploads.UploadedFile")
def on_file_saved(sender, instance, created, **kwargs):
    """
    Push a WS notification whenever a file transitions to parsed or parse_error.
    Only fires on updates (not on initial creation) to avoid noise.
    """
    if created:
        return

    status = instance.parse_status
    if status not in ("parsed", "parse_error"):
        return

    user = getattr(instance.batch, "uploaded_by", None)
    if not user:
        return

    try:
        from chat.notify import notify_user, notify_file_processed

        ws_status = "parsed" if status == "parsed" else "error"
        notify_file_processed(
            user.pk,
            file_id=instance.pk,
            filename=instance.original_filename,
            status=ws_status,
        )

        title   = "File processed" if status == "parsed" else "File processing failed"
        message = (
            f"'{instance.original_filename}' was parsed successfully."
            if status == "parsed"
            else f"'{instance.original_filename}' could not be parsed: {instance.parse_error[:120]}"
        )
        notify_user(user.pk, title=title, message=message)

    except Exception:
        pass  # Never let a signal crash the request
