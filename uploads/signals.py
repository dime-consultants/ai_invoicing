# uploads/signals.py
"""
Django signals for the uploads app.
Fires WebSocket notifications when a file's parse status changes.

Import note
-----------
chat.notify is imported INSIDE the receiver body (not at module level) to
avoid the circular import:  chat.services → uploads.models → uploads.signals
→ chat.notify → (chat module) → chat.services
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

    file_status = instance.parse_status
    if file_status not in ("parsed", "parse_error"):
        return

    user = getattr(instance.batch, "uploaded_by", None)
    if not user:
        return

    try:
        # Lazy import breaks the chat → uploads → chat circular dependency.
        from chat.notify import notify_user, notify_file_processed

        ws_status = "parsed" if file_status == "parsed" else "error"
        notify_file_processed(
            user.pk,
            file_id=instance.pk,
            filename=instance.original_filename,
            status=ws_status,
        )

        title   = "File processed" if file_status == "parsed" else "File processing failed"
        message = (
            f"'{instance.original_filename}' was parsed successfully."
            if file_status == "parsed"
            else f"'{instance.original_filename}' could not be parsed: {instance.parse_error[:120]}"
        )
        notify_user(user.pk, title=title, message=message)

    except Exception:
        pass  # Never let a signal crash the request