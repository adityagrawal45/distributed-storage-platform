"""
ProcessedEvent persistence (Phase 8) — consumer idempotency.

Two methods, and the relationship between them is the whole design:

- `has_processed()` is an **optimization**. It answers "has this consumer
  already handled this event_id?" with a cheap indexed SELECT so the
  common redelivery skips expensive work (a GCS download, a Pillow
  decode) entirely.

- `record()` is the **guarantee**. It attempts the INSERT inside a
  SAVEPOINT (`begin_nested`), so when two replicas both passed the
  pre-check and race, the loser's `UniqueConstraint(event_id,
  consumer_name)` violation rolls back only that savepoint — not the
  worker's whole unit of work — and is reported back to the caller as
  `created=False` rather than as a request-ending `IntegrityError`.

This is deliberately the same shape as Phase 6's
`UploadChunkRepository.create_or_get_existing`, for the same reason: a
lock or a pre-check makes a race rare, but only a database constraint
makes it safe.

What the caller must do with `created=False`
--------------------------------------------
**ACK.** Never NACK. A `created=False` means another replica already
recorded a terminal outcome for this exact event — the work is done. A
NACK would ask Pub/Sub to redeliver work that has definitionally already
succeeded, which at best wastes a delivery attempt and at worst
crash-loops the worker against the same duplicate forever. See
`app/workers/base.py::BaseWorker._handle`.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.repositories.base import BaseRepository


class ProcessedEventRepository(BaseRepository[ProcessedEvent]):
    model = ProcessedEvent

    async def get(self, event_id: uuid.UUID, consumer_name: str) -> ProcessedEvent | None:
        result = await self._session.execute(
            select(ProcessedEvent).where(
                ProcessedEvent.event_id == event_id,
                ProcessedEvent.consumer_name == consumer_name,
            )
        )
        return result.scalar_one_or_none()

    async def has_processed(self, event_id: uuid.UUID, consumer_name: str) -> bool:
        """
        Pre-check only — NOT the idempotency guarantee (see module
        docstring). A `False` here does not promise the INSERT will win.
        """
        return await self.get(event_id, consumer_name) is not None

    async def record(
        self,
        *,
        event_id: uuid.UUID,
        consumer_name: str,
        status: ProcessedEventStatus,
        error: str | None = None,
    ) -> tuple[ProcessedEvent, bool]:
        """
        Records a terminal outcome. Returns `(row, created)`.

        `created=False` means a concurrent replica won the race and its
        row is returned instead. The caller ACKs either way.
        """
        entry = ProcessedEvent(
            event_id=event_id,
            consumer_name=consumer_name,
            status=status,
            error=error[:2000] if error else None,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(entry)
                await self._session.flush()
            return entry, True
        except IntegrityError:
            existing = await self.get(event_id, consumer_name)
            if existing is None:  # pragma: no cover - defensive; the violation implies a row exists
                raise
            return existing, False

    async def list_failures(self, consumer_name: str) -> list[ProcessedEvent]:
        """
        Every permanently-failed event for one consumer.

        This is the operational answer to "why does this file have no
        thumbnail?" — non-retryable failures ACK and are recorded here
        rather than being routed to the DLQ (see `NonRetryableEventError`).
        """
        result = await self._session.execute(
            select(ProcessedEvent)
            .where(
                ProcessedEvent.consumer_name == consumer_name,
                ProcessedEvent.status == ProcessedEventStatus.FAILED,
            )
            .order_by(ProcessedEvent.processed_at)
        )
        return list(result.scalars().all())
