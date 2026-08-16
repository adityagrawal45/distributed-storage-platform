"""
OutboxEvent persistence (Phase 8).

The atomicity contract
----------------------
`add()` is the whole point of this repository, and it is deliberately
boring: `session.add()` + `flush()`, exactly like every other repository
in this codebase. It does **not** commit, does not open its own
transaction, and does not manage a session of its own.

That is what makes the outbox atomic. The only `session.commit()` in the
entire application is in `app/database/session.py::get_db`, at the
request boundary. Because this repository is constructed from that same
request-scoped session (see `get_outbox_repository` in
`app/dependencies/providers.py`), an outbox row inserted while handling
`POST /files/upload` is part of the *same* transaction as the
`FileMetadata` row it describes. Either both commit or neither does.
There is no new transaction-management code anywhere in Phase 8 — the
existing Unit of Work already provides the guarantee, and the outbox
pattern is precisely the trick of exploiting that instead of inventing a
two-phase commit.

Publisher-side query design
---------------------------
`fetch_pending_batch` uses `FOR UPDATE SKIP LOCKED`:

  SELECT ... WHERE status IN (PENDING, FAILED) AND next_attempt_at <= now()
  ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED

- `FOR UPDATE` locks the selected rows so a second publisher cannot pick
  them up concurrently.
- `SKIP LOCKED` makes that second publisher skip past locked rows and
  take the *next* available ones instead of blocking behind them.

Phase 8 runs a single outbox-publisher replica, so strictly speaking
neither is needed today. They are here anyway because the alternative —
discovering under load that scaling the publisher to 2 replicas double-
publishes everything — is a much worse thing to find out later, and the
cost at one replica is nil. `SKIP LOCKED` is a Postgres feature; SQLite
(the test backing store) ignores the clause, which is harmless because
the tests exercise one publisher at a time.

Ordering is by `created_at`, giving best-effort chronological publishing.
It is explicitly NOT a total ordering guarantee: rows are published
per-row and independently, and Pub/Sub without ordering keys does not
preserve order anyway (see `app/events/topics.py` for why no consumer
needs it).
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.models.outbox_event import OutboxEvent, OutboxEventStatus
from app.repositories.base import BaseRepository


def _as_aware(value: datetime | None) -> datetime | None:
    """
    Normalizes a DB-loaded datetime to tz-aware UTC.

    SQLite (the test backing store) round-trips `DateTime(timezone=True)`
    values as NAIVE datetimes, unlike Postgres — comparing one directly
    against `datetime.now(timezone.utc)` raises
    `TypeError: can't compare offset-naive and offset-aware datetimes`.
    This is the same trap `ChunkedUploadService._is_expired` documents
    from Phase 6; the publisher hits it on `next_attempt_at`.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class OutboxRepository(BaseRepository[OutboxEvent]):
    model = OutboxEvent

    async def add_event(
        self,
        *,
        event_id: uuid.UUID,
        event_type: str,
        event_version: int,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        correlation_id: uuid.UUID,
        causation_id: uuid.UUID | None,
        user_id: uuid.UUID,
        payload: dict,
    ) -> OutboxEvent:
        """
        Inserts one PENDING outbox row into the CALLER'S transaction.

        Flushes (so the row gets its PK and is visible to later reads in
        the same request) but never commits — see the module docstring.
        """
        event = OutboxEvent(
            event_id=event_id,
            event_type=event_type,
            event_version=event_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            user_id=user_id,
            payload=payload,
            status=OutboxEventStatus.PENDING,
            attempt_count=0,
        )
        return await self.add(event)

    async def get_by_event_id(self, event_id: uuid.UUID) -> OutboxEvent | None:
        result = await self._session.execute(select(OutboxEvent).where(OutboxEvent.event_id == event_id))
        return result.scalar_one_or_none()

    async def fetch_pending_batch(self, limit: int | None = None) -> list[OutboxEvent]:
        """
        Claims up to `limit` rows that are due for a publish attempt.

        FAILED rows are included alongside PENDING ones: FAILED means
        "the last attempt failed, retry after `next_attempt_at`", not
        "give up". See `OutboxEventStatus`.
        """
        settings = get_settings()
        batch_size = limit or settings.OUTBOX_BATCH_SIZE
        now = datetime.now(timezone.utc)

        statement = (
            select(OutboxEvent)
            .where(
                OutboxEvent.status.in_([OutboxEventStatus.PENDING, OutboxEventStatus.FAILED]),
                OutboxEvent.next_attempt_at <= now,
            )
            .order_by(OutboxEvent.created_at)
            .limit(batch_size)
        )
        # `SKIP LOCKED` is Postgres-only; SQLite silently ignores the
        # whole FOR UPDATE clause, which is fine for the single-publisher
        # test scenarios. Guarded rather than assumed so the intent is
        # explicit at the call site.
        if self._session.bind is not None and self._session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)

        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def list_by_status(self, status: OutboxEventStatus) -> list[OutboxEvent]:
        """Diagnostic/read helper — used by tests and by the operational runbook's queries."""
        result = await self._session.execute(
            select(OutboxEvent).where(OutboxEvent.status == status).order_by(OutboxEvent.created_at)
        )
        return list(result.scalars().all())

    async def mark_published(self, event: OutboxEvent) -> OutboxEvent:
        """
        Marks a row PUBLISHED after Pub/Sub accepted it.

        `published_at` is set explicitly here rather than relying on the
        column's `onupdate` alone, so the value is correct even for a row
        whose only changing column is `status`.
        """
        event.status = OutboxEventStatus.PUBLISHED
        event.published_at = datetime.now(timezone.utc)
        event.last_error = None
        await self.flush()
        return event

    async def mark_failed(self, event: OutboxEvent, error: str) -> OutboxEvent:
        """
        Records a failed publish attempt and schedules the next one with
        exponential backoff:

            delay = min(BASE * 2 ** (attempt_count - 1), MAX)

        Backoff matters here because the dominant failure mode is "Pub/Sub
        (or the network to it) is unavailable" — which affects EVERY row
        at once. Without backoff the publisher would spin at
        `OUTBOX_POLL_INTERVAL` against a service that is already
        struggling, turning an outage into a self-inflicted retry storm.
        The row's `last_error` is truncated so one pathological exception
        message cannot bloat the table.
        """
        settings = get_settings()
        event.attempt_count = (event.attempt_count or 0) + 1
        event.status = OutboxEventStatus.FAILED
        event.last_error = error[:2000]
        # Explicitly pinned to None. `published_at` carries a column-level
        # `onupdate` (Python-side, for the `MissingGreenlet` reason in the
        # model's docstring), and a column `onupdate` fires on ANY update
        # to the row — including this one. Without this line a *failed*
        # publish would stamp a publish time, which would be a lie in the
        # one table an operator reads during an incident. An explicit
        # assignment takes precedence over `onupdate` at flush time.
        event.published_at = None

        delay_seconds = min(
            settings.OUTBOX_RETRY_BASE_DELAY_SECONDS * (2 ** (event.attempt_count - 1)),
            settings.OUTBOX_RETRY_MAX_DELAY_SECONDS,
        )
        event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        await self.flush()
        return event

    @staticmethod
    def is_due(event: OutboxEvent, *, now: datetime | None = None) -> bool:
        """tz-safe 'is this row eligible for a publish attempt right now?' (see `_as_aware`)."""
        reference = now or datetime.now(timezone.utc)
        next_attempt = _as_aware(event.next_attempt_at)
        return next_attempt is None or next_attempt <= reference
