"""
File processing worker — the first consumer in the chain.

Run with: `python -m app.workers.file_processing_worker`

What it does
------------
Consumes `file.uploaded` (Phase 3's single-shot path) and `file.completed`
(Phase 6's chunked path) from FILE_EVENTS_TOPIC and:

1. **Verifies the bytes really landed.** The event was written inside the
   API's transaction, which proves the *metadata* committed — it does not
   prove GCS still has the object (a lifecycle rule, a botched cleanup, a
   rollback that half-succeeded). One cheap metadata HEAD closes that gap
   asynchronously, off the upload's critical path, which is precisely the
   kind of work this phase exists to move off the request.
2. **Cross-checks size and content type** against what the event claimed,
   logging a discrepancy loudly. It does NOT "fix" the row: a mismatch
   between metadata and bytes is a fact for a human to look at, and
   silently rewriting one side to agree with the other would destroy the
   evidence.
3. **Fans out** by publishing `thumbnail.requested` (only for a supported
   image content type) and `notification.requested`.

Why the fan-out is published DIRECTLY, not through the outbox
-------------------------------------------------------------
The outbox exists to solve the dual-write problem: a Postgres write and a
Pub/Sub publish that must be atomic with each other. At this point in the
chain there is **no competing Postgres write** — this worker validates
and forwards, it does not mutate business data. With nothing to be atomic
*with*, an outbox row would add a table write, a poll interval of latency
and a second process's involvement, and buy nothing.

The failure mode of publishing directly is bounded and already handled:
if the fan-out publish fails, the whole message NACKs and Pub/Sub
redelivers, and because the downstream consumers deduplicate on
`event_id`, a partial fan-out (thumbnail published, notification failed)
that gets fully retried causes one absorbed duplicate rather than double
work.

Idempotency of the follow-up events
-----------------------------------
The derived events' `event_id`s are **deterministic** — UUIDv5 over the
parent event id and the child event type. A redelivery that re-runs the
fan-out therefore publishes the SAME `event_id` the first attempt did, so
the thumbnail/notification workers recognize it as a duplicate through
their normal `ProcessedEvent` path. A random `uuid4()` would produce a
fresh identity per retry and defeat downstream deduplication entirely —
this is the subtle bug the whole idempotency design is easiest to lose to.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.gcs import get_storage_client
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.exceptions.custom_exceptions import (
    NonRetryableEventError,
    RetryableEventError,
    StorageObjectNotFoundException,
)
from app.logging.logger import get_logger
from app.services.storage_service import StorageService
from app.workers.base import BaseWorker

logger = get_logger(__name__)

WORKER_NAME = "file-processing-worker"

# Stable namespace for deterministic derived event IDs. A fixed constant,
# not a generated one, so the same parent event always derives the same
# child id across processes, restarts and deployments.
DERIVED_EVENT_NAMESPACE = uuid.UUID("6f5f2a2e-9f3d-4a3a-8c1e-0f3f8c2b7d41")


def derive_event_id(parent_event_id: uuid.UUID, child_event_type: EventType) -> uuid.UUID:
    """Deterministic child event id — see the module docstring on why this must not be random."""
    return uuid.uuid5(DERIVED_EVENT_NAMESPACE, f"{parent_event_id}:{child_event_type.value}")


class FileProcessingWorker(BaseWorker):
    worker_name = WORKER_NAME
    consumer_name = WORKER_NAME

    #: Both upload paths converge here so downstream consumers need one
    #: code path rather than one per upload mechanism.
    HANDLED_EVENT_TYPES = frozenset({EventType.FILE_UPLOADED, EventType.FILE_COMPLETED})

    def __init__(
        self,
        *,
        storage_service: StorageService | None = None,
        publisher: EventPublisher | None = None,
        **kwargs,
    ):
        settings = get_settings()
        self._settings = settings
        self._storage = storage_service
        self._publisher = publisher or EventPublisher()
        super().__init__(**kwargs)

    def default_subscription(self) -> str:
        return get_settings().FILE_WORKER_SUBSCRIPTION

    def interested_in(self, envelope: EventEnvelope) -> bool:
        """
        FILE_EVENTS_TOPIC also carries folder/rename/move/thumbnail
        events this worker has no business with. Filtering client-side
        keeps the subscription simple; a Pub/Sub subscription filter on
        the `event_type` attribute would move it server-side later with
        no code change, which is why the envelope exposes it as an
        attribute in the first place.
        """
        return envelope.event_type in self.HANDLED_EVENT_TYPES

    def _resolve_storage(self) -> StorageService:
        if self._storage is None:
            self._storage = StorageService(get_storage_client())
        return self._storage

    def _is_thumbnailable(self, content_type: str | None) -> bool:
        return bool(content_type) and content_type.lower() in self._settings.THUMBNAIL_SUPPORTED_CONTENT_TYPES

    async def process(self, envelope: EventEnvelope, session: AsyncSession) -> None:
        payload = envelope.payload
        object_name = payload.get("object_name")
        file_id = payload.get("file_id")

        if not object_name or not file_id:
            # The producer is this same codebase, so a payload missing its
            # own required fields is a bug, not a transient condition —
            # redelivery cannot add the field back.
            raise NonRetryableEventError(
                f"Event {envelope.event_id} is missing required payload fields (file_id/object_name)."
            )

        blob = await self._verify_object(object_name)
        self._cross_check(payload, blob, file_id=file_id, object_name=object_name)

        await self._fan_out(envelope, payload, file_id=file_id, object_name=object_name)

    # ------------------------------------------------------------------
    # Step 1 & 2 — verification
    # ------------------------------------------------------------------
    async def _verify_object(self, object_name: str):
        try:
            return await self._resolve_storage().get_blob_metadata(object_name)
        except StorageObjectNotFoundException as exc:
            # Permanent: the bytes are gone. Redelivering will not bring
            # them back, and the FAILED ProcessedEvent row is the durable
            # record an operator queries. This is exactly the case the DLQ
            # is NOT for.
            raise NonRetryableEventError(
                f"Storage object '{object_name}' does not exist; the metadata row references missing bytes."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Everything else about GCS (timeouts, 5xx, auth blips) is
            # transient by default — NACK and let Pub/Sub redeliver.
            raise RetryableEventError(f"Could not read storage metadata for '{object_name}': {exc}") from exc

    @staticmethod
    def _cross_check(payload: dict, blob, *, file_id: str, object_name: str) -> None:
        """
        Compares what the event claimed against what storage actually
        holds. Logs, never mutates: a metadata/bytes disagreement is
        evidence, and rewriting either side to agree would destroy it.
        """
        expected_size = payload.get("size")
        actual_size = getattr(blob, "size", None)
        if expected_size is not None and actual_size is not None and int(expected_size) != int(actual_size):
            logger.error(
                "file_size_mismatch",
                file_id=file_id,
                object_name=object_name,
                expected_size=expected_size,
                actual_size=actual_size,
            )

        expected_type = payload.get("content_type")
        actual_type = getattr(blob, "content_type", None)
        if expected_type and actual_type and expected_type.lower() != actual_type.lower():
            logger.warning(
                "file_content_type_mismatch",
                file_id=file_id,
                object_name=object_name,
                expected_content_type=expected_type,
                actual_content_type=actual_type,
            )

    # ------------------------------------------------------------------
    # Step 3 — fan-out
    # ------------------------------------------------------------------
    def _derived(self, parent: EventEnvelope, event_type: EventType, payload: dict) -> EventEnvelope:
        return EventEnvelope(
            event_id=derive_event_id(parent.event_id, event_type),
            event_type=event_type,
            producer=WORKER_NAME,
            # Same correlation chain (this is still the user's original
            # operation); causation points at the event that caused it.
            correlation_id=parent.correlation_id,
            causation_id=parent.event_id,
            user_id=parent.user_id,
            payload=payload,
        )

    async def _fan_out(
        self, envelope: EventEnvelope, payload: dict, *, file_id: str, object_name: str
    ) -> None:
        content_type = payload.get("content_type")
        derived: list[EventEnvelope] = []

        if self._is_thumbnailable(content_type):
            derived.append(
                self._derived(
                    envelope,
                    EventType.THUMBNAIL_REQUESTED,
                    {
                        "file_id": file_id,
                        "object_name": object_name,
                        "content_type": content_type,
                        "bucket_name": payload.get("bucket_name"),
                    },
                )
            )
        else:
            # Not an error and not a failure — most files are not images.
            logger.info("thumbnail_not_requested_unsupported_type", file_id=file_id, content_type=content_type)

        derived.append(
            self._derived(
                envelope,
                EventType.NOTIFICATION_REQUESTED,
                {
                    "notification_type": "file_ready",
                    "file_id": file_id,
                    "filename": payload.get("filename"),
                    "size": payload.get("size"),
                },
            )
        )

        try:
            for child in derived:
                await self._publisher.publish(child)
        except Exception as exc:  # noqa: BLE001
            # Retryable: the whole message is redelivered and the fan-out
            # re-runs. Deterministic derived event IDs make the repeat
            # harmless — downstream absorbs it as a duplicate.
            raise RetryableEventError(f"Fan-out publish failed: {exc}") from exc

        logger.info(
            "file_processed",
            file_id=file_id,
            object_name=object_name,
            derived_events=[e.event_type.value for e in derived],
        )


def main() -> None:  # pragma: no cover - process entrypoint
    from app.logging.logger import configure_logging

    configure_logging()
    asyncio.run(FileProcessingWorker().run())


if __name__ == "__main__":  # pragma: no cover
    main()
