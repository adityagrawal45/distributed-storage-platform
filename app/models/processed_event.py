"""
ProcessedEvent ORM model (Phase 8) — the consumer-side idempotency ledger.

Pub/Sub delivery is **at-least-once**. So is the transactional outbox in
front of it (see `app/models/outbox_event.py`: a publisher crash between
"Pub/Sub accepted the message" and "commit `mark_published`" republishes
the row). Duplicate delivery is therefore not an edge case to be
defended against defensively — it is the documented, expected behavior
of the transport, and every consumer must be correct under it.

This table is how. One row per `(event_id, consumer_name)`:

- `event_id` (not the Pub/Sub `message_id`) is the key, because Pub/Sub
  assigns a NEW `message_id` to every redelivery of the same logical
  event. Deduplicating on `message_id` would deduplicate nothing.
- `consumer_name` is part of the key because several independent
  consumers legitimately process the same event — the file-processing
  worker and (in a future phase) an audit projection both consume
  `file.uploaded`, and one having finished must not make the other skip
  its work.

The `UniqueConstraint` is the ACTUAL guarantee
----------------------------------------------
`BaseWorker._handle` runs a SELECT before processing ("have I seen this
already?"). That query is a **performance optimization only** — it lets
the common duplicate skip the expensive work. It is not the safety
mechanism, because two replicas can both pass the SELECT before either
INSERTs. The database's unique constraint is what actually holds, and
the losing replica's `IntegrityError` is caught, logged as a duplicate,
and **ACKed** — never NACKed. NACKing there would be actively wrong: the
winning replica's equivalent work already succeeded, so redelivering the
message asks for work that is definitionally already done.

This is the same "a real DB constraint is the guarantee; the pre-check is
an optimization" structure Phase 6 established for
`UploadChunk`'s `UniqueConstraint(upload_id, chunk_number)`.

No mixins, for the same reasons as `UploadChunk`/`OutboxEvent`:
append-only, high-write, no soft-delete concept, no audit provenance
beyond the columns already present.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class ProcessedEventStatus(str, Enum):
    """
    SUCCEEDED — the consumer completed its work for this event.
    FAILED    — the consumer hit a NON-retryable error (see
                `NonRetryableEventError`). The message was ACKed; this row
                plus its `error` text is the durable record of why, and
                the thing an operator queries when a user asks "why does
                my file have no thumbnail?". Retryable failures write NO
                row at all — they NACK and are retried, so recording them
                would mean recording an outcome that has not happened yet.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer_name", name="uq_processed_events_event_consumer"),
        # Supports the pre-check SELECT; also the natural lookup order
        # for "what did consumer X do with event Y".
        Index("ix_processed_events_consumer", "consumer_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    consumer_name: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[ProcessedEventStatus] = mapped_column(
        SAEnum(
            ProcessedEventStatus,
            name="processed_event_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProcessedEvent event_id={self.event_id} consumer={self.consumer_name!r} status={self.status.value}>"
