"""
OutboxEvent ORM model (Phase 8) — the transactional outbox.

Why this table exists
---------------------
Publishing a domain event and writing the business data it describes are
two writes to two different systems (Postgres and Pub/Sub). There is no
distributed transaction between them, so a naive
"`session.commit()`; then `publisher.publish()`" has two failure modes,
both real:

  * publish succeeds, commit fails  -> consumers act on a file that does
                                       not exist. Unrecoverable: the
                                       message is already gone.
  * commit succeeds, publish fails  -> the file exists but nothing
                                       downstream ever hears about it.
                                       Silent, and invisible until a user
                                       asks why their thumbnail never
                                       appeared.

The outbox collapses both writes into ONE Postgres transaction: the
event row is inserted through the ordinary session-bound repository
(`OutboxRepository.add`, which only `flush()`es — never commits), so it
rides along in the exact same request-scoped transaction that
`app/database/session.py::get_db` commits at the request boundary. Either
the business row and the event row both exist, or neither does. A
separate poller (`app/workers/outbox_publisher.py`) then moves rows from
Postgres to Pub/Sub, retrying until it succeeds.

The residual weakness is deliberate and bounded: the publisher can crash
*after* Pub/Sub accepted a message but *before* `mark_published` commits,
so the row is republished on the next poll. That makes delivery
at-least-once, which is why every consumer deduplicates on `event_id`
via `ProcessedEvent`'s unique constraint. At-least-once + idempotent
consumers is a correct system; exactly-once across two systems is not
achievable without both participating in the same transaction.

Model design decisions
----------------------
- **No mixins**, following `app/models/upload_chunk.py`'s documented
  precedent. This is an append-only, high-write log row: it has no
  "trashed" state (a published row is either retained for audit or
  reaped by a future retention job, never soft-deleted), and no
  `created_by`/`updated_by` provenance beyond `user_id` + `aggregate_id`,
  which are already columns. `AuditMixin`/`SoftDeleteMixin` would be
  eight columns of schema weight on the hottest-write table in the
  system, with no reader.

- **`published_at` uses a Python-side `onupdate`**, never
  `onupdate=func.now()`. This is not a style preference: a server-side
  `onupdate` on a column that is mutated and then re-serialized inside
  the same async request raises `MissingGreenlet`. That bug was
  previously hit in this codebase on `AuditMixin.updated_at` (see
  CONTEXT.md's History section) and again anticipated on
  `UploadChunk.updated_at`. The publisher worker marks a row published
  and then logs/serializes it in the same unit of work, so the exact same
  trap applies here.

- **`status` + `next_attempt_at` compound index.** The publisher's hot
  query is `WHERE status IN (...) AND next_attempt_at <= now() ORDER BY
  created_at`, run every `OUTBOX_POLL_INTERVAL` seconds forever. Without
  this index that is a sequential scan over a table that grows with every
  write in the system.

- **`aggregate_type`/`aggregate_id` are captured even though nothing
  reads them in Phase 8.** They cost two columns and make two future
  capabilities free: per-aggregate ordering keys (see
  `app/events/topics.py`'s ordering analysis) and "replay every event
  for file X" during an incident. Backfilling them later would require
  reconstructing them from payloads.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, DateTime, Enum as SAEnum, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class OutboxEventStatus(str, Enum):
    """
    Lifecycle of one outbox row.

    FAILED is NOT terminal — it means "the last publish attempt failed,
    try again after `next_attempt_at`". The publisher's fetch query
    selects PENDING *and* FAILED for exactly that reason. A row that can
    never be published (e.g. an event type with no topic mapping) stays
    FAILED with its `last_error` set and its backoff growing, which is
    visible and queryable rather than silently dropped.
    """

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


def _published_at_on_update(context) -> datetime | None:
    """
    Python-side `onupdate` for `OutboxEvent.published_at`.

    Returns "now" only when this UPDATE is setting `status` to PUBLISHED;
    for every other update (notably `mark_failed`) it returns the value
    already being written, leaving the column untouched. See the column
    definition for why a bare `lambda: now()` is wrong here, and the
    module docstring for why a server-side `func.now()` is wrong
    everywhere in this codebase.
    """
    parameters = context.get_current_parameters()
    status = parameters.get("status")
    status_value = getattr(status, "value", status)
    if status_value == OutboxEventStatus.PUBLISHED.value:
        return datetime.now(timezone.utc)
    return parameters.get("published_at")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        # The publisher's polling query, in one index.
        Index("ix_outbox_events_status_next_attempt", "status", "next_attempt_at"),
        # "Show me everything that ever happened to this file/folder."
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The envelope's `event_id`. Unique because it is the consumer-side
    # idempotency key — two outbox rows sharing one event_id would let a
    # consumer legitimately discard the second as a duplicate.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4)

    # Stored as a plain string, not a native enum: the event catalog grows
    # every phase, and an enum type would make adding one event type a
    # migration with a lock on this table. `EventType(value)` validates it
    # at the boundary instead.
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # JSONB (not JSON, not TEXT): queryable during an incident
    # (`payload->>'file_id'`) and stored pre-parsed, so a replay tool does
    # not re-parse every row. `.with_variant(JSON)` keeps the SQLite test
    # backing store working — same cross-dialect accommodation the suite
    # already relies on for native enums.
    payload: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )

    status: Mapped[OutboxEventStatus] = mapped_column(
        SAEnum(
            OutboxEventStatus,
            name="outbox_event_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=OutboxEventStatus.PENDING,
        nullable=False,
        server_default=OutboxEventStatus.PENDING.value,
        index=True,
    )

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Both a Python-side `default` and a `server_default`: the server
    # default keeps rows inserted by raw SQL/replay tooling correct, while
    # the Python default guarantees the in-session object carries a
    # tz-AWARE value immediately — SQLite (the test backing store) hands
    # `DateTime(timezone=True)` values back naive, and the publisher
    # compares `next_attempt_at` against `datetime.now(timezone.utc)`.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        # Python-side — see module docstring. NEVER `func.now()`, which
        # would reintroduce the async `MissingGreenlet` crash on the
        # publisher's mark-then-log-the-row path.
        #
        # Context-aware rather than a bare `lambda: now()` because a
        # column `onupdate` fires on ANY update to the row: a bare
        # callable would stamp a publish time onto a row whose publish
        # just FAILED, which is a lie in the one table an operator reads
        # during an incident. This variant stamps the time only when the
        # row is transitioning to PUBLISHED, and otherwise preserves
        # whatever was there.
        onupdate=lambda context: _published_at_on_update(context),
    )
    # Defaults to "now" rather than NULL so a brand-new row is immediately
    # eligible on the very next poll; a failed row's backoff pushes it out.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OutboxEvent event_type={self.event_type!r} status={self.status.value} attempts={self.attempt_count}>"
