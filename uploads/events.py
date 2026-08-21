import logging

from asgiref.sync import sync_to_async
from .models import PipelineEvent

logger = logging.getLogger(__name__)


async def emit(
    event_type: str,
    *,
    batch,
    file=None,
    payload: dict | None = None,
) -> None:

    event = await sync_to_async(
        PipelineEvent.objects.create,
        thread_sensitive=True,
    )(
        batch=batch,
        file=file,
        event_type=event_type,
        payload=payload or {},
    )

    await _relay_after_commit(event)


async def _relay_after_commit(event: PipelineEvent) -> None:
    try:
        await _push_to_channel(event)
    except Exception:
        logger.exception(
            "Failed to relay event %s (id=%s) — client can still catch up via "
            "GET /batches/<id>/events",
            event.event_type,
            event.pk,
        )


async def _push_to_channel(event: PipelineEvent) -> None:
    from channels.layers import get_channel_layer

    layer = get_channel_layer()

    if layer is None:
        return

    await layer.group_send(
        f"batch_{event.batch.public_id}",
        {
            "type": "pipeline.event",
            "event": {
                "eventType": event.event_type,
                "fileId": (
                    event.file.public_id
                    if event.file
                    else None
                ),
                "batchId": str(event.batch.public_id),
                "payload": event.payload,
                "createdAt": event.created_at.isoformat(),
            },
        },
    )