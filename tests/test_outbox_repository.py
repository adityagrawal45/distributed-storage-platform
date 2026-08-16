"""
Phase 8 — OutboxRepository and ProcessedEventRepository tests.

These run against the same in-memory SQLite `db_session` fixture every
other repository-level test uses. Two Postgres-only behaviors are
therefore NOT exercised here and are called out honestly rather than
faked: `FOR UPDATE SKIP LOCKED` (SQLite ignores it) and JSONB operator
querying. What IS exercised is everything the application logic depends
on: status transitions, backoff arithmetic, due-ness filtering, batch
limits, and — most importantly — that `add_event` never commits.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.events.envelope import EventType
from app.models.outbox_event import OutboxEvent, OutboxEventStatus
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.processed_event_repository import ProcessedEventRepository


async def _add(repo: OutboxRepository, **overrides) -> OutboxEvent:
    kwargs = {
        "event_id": uuid.uuid4(),
        "event_type": EventType.FILE_UPLOADED.value,
        "event_version": 1,
        "aggregate_type": "file",
        "aggregate_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
        "causation_id": None,
        "user_id": uuid.uuid4(),
        "payload": {"file_id": "abc", "size": 12},
    }
    kwargs.update(overrides)
    return await repo.add_event(**kwargs)


# ---------------------------------------------------------------------
# add_event / atomicity
# ---------------------------------------------------------------------
async def test_add_event_persists_a_pending_row(db_session):
    repo = OutboxRepository(db_session)
    event = await _add(repo)

    assert event.status == OutboxEventStatus.PENDING
    assert event.attempt_count == 0
    assert event.published_at is None
    assert event.last_error is None
    assert event.payload == {"file_id": "abc", "size": 12}


async def test_add_event_flushes_but_does_not_commit(db_session):
    """
    The atomicity contract: the row must be visible inside the caller's
    transaction and must vanish if that transaction rolls back. If
    `add_event` committed, the rollback below would not remove it — and
    the outbox would no longer be atomic with the business write.
    """
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    event_id = event.event_id

    # Visible pre-commit, inside the same transaction.
    assert await repo.get_by_event_id(event_id) is not None

    await db_session.rollback()

    result = await db_session.execute(select(OutboxEvent).where(OutboxEvent.event_id == event_id))
    assert result.scalar_one_or_none() is None


async def test_event_id_is_unique(db_session):
    from sqlalchemy.exc import IntegrityError

    repo = OutboxRepository(db_session)
    shared = uuid.uuid4()
    await _add(repo, event_id=shared)
    with pytest.raises(IntegrityError):
        await _add(repo, event_id=shared)
    await db_session.rollback()


# ---------------------------------------------------------------------
# fetch_pending_batch
# ---------------------------------------------------------------------
async def test_fetch_pending_batch_returns_due_pending_rows(db_session):
    repo = OutboxRepository(db_session)
    await _add(repo)
    await _add(repo)

    batch = await repo.fetch_pending_batch()
    assert len(batch) == 2


async def test_fetch_pending_batch_includes_failed_rows_because_failed_is_not_terminal(db_session):
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    await repo.mark_failed(event, "transient boom")
    # Wind the backoff back so it is due again.
    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await repo.flush()

    batch = await repo.fetch_pending_batch()
    assert [e.id for e in batch] == [event.id]
    assert batch[0].status == OutboxEventStatus.FAILED


async def test_fetch_pending_batch_excludes_published_rows(db_session):
    repo = OutboxRepository(db_session)
    published = await _add(repo)
    pending = await _add(repo)
    await repo.mark_published(published)

    batch = await repo.fetch_pending_batch()
    assert [e.id for e in batch] == [pending.id]


async def test_fetch_pending_batch_excludes_rows_not_yet_due(db_session):
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    event.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await repo.flush()

    assert await repo.fetch_pending_batch() == []


async def test_fetch_pending_batch_respects_the_limit(db_session):
    repo = OutboxRepository(db_session)
    for _ in range(5):
        await _add(repo)

    assert len(await repo.fetch_pending_batch(limit=2)) == 2


async def test_fetch_pending_batch_defaults_to_the_configured_batch_size(db_session):
    repo = OutboxRepository(db_session)
    for _ in range(3):
        await _add(repo)

    assert get_settings().OUTBOX_BATCH_SIZE >= 3
    assert len(await repo.fetch_pending_batch()) == 3


# ---------------------------------------------------------------------
# mark_published / mark_failed
# ---------------------------------------------------------------------
async def test_mark_published_sets_status_and_timestamp_and_clears_error(db_session):
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    await repo.mark_failed(event, "earlier failure")
    await repo.mark_published(event)

    assert event.status == OutboxEventStatus.PUBLISHED
    assert event.published_at is not None
    assert event.last_error is None


async def test_published_at_survives_serialization_in_the_same_unit_of_work(db_session):
    """
    Regression guard for the `MissingGreenlet` trap documented in
    CONTEXT.md: a server-side `onupdate=func.now()` on a column mutated
    and then read back in the same async unit of work raises. A
    Python-side onupdate does not — and the value must be a real
    datetime, not an unevaluated SQL construct.
    """
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    await repo.mark_published(event)

    assert isinstance(event.published_at, datetime)
    assert event.published_at.tzinfo is not None


async def test_mark_failed_increments_attempts_and_applies_exponential_backoff(db_session):
    settings = get_settings()
    repo = OutboxRepository(db_session)
    event = await _add(repo)

    before = datetime.now(timezone.utc)
    await repo.mark_failed(event, "pubsub unavailable")

    assert event.status == OutboxEventStatus.FAILED
    assert event.attempt_count == 1
    assert event.last_error == "pubsub unavailable"

    first_delay = (event.next_attempt_at - before).total_seconds()
    assert first_delay == pytest.approx(settings.OUTBOX_RETRY_BASE_DELAY_SECONDS, abs=1.0)

    before = datetime.now(timezone.utc)
    await repo.mark_failed(event, "still unavailable")
    assert event.attempt_count == 2
    second_delay = (event.next_attempt_at - before).total_seconds()
    assert second_delay > first_delay


async def test_backoff_is_capped_at_the_configured_maximum(db_session):
    settings = get_settings()
    repo = OutboxRepository(db_session)
    event = await _add(repo)

    for _ in range(20):
        await repo.mark_failed(event, "down")

    delay = (event.next_attempt_at - datetime.now(timezone.utc)).total_seconds()
    assert delay <= settings.OUTBOX_RETRY_MAX_DELAY_SECONDS + 1


async def test_last_error_is_truncated_so_one_bad_row_cannot_bloat_the_table(db_session):
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    await repo.mark_failed(event, "x" * 10_000)
    assert len(event.last_error) == 2000


async def test_is_due_normalizes_naive_timestamps_from_sqlite(db_session):
    """The Phase 6 SQLite-naive-datetime trap, guarded at the repository boundary."""
    repo = OutboxRepository(db_session)
    event = await _add(repo)
    event.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    assert repo.is_due(event) is True

    event.next_attempt_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None)
    assert repo.is_due(event) is False


async def test_list_by_status_filters(db_session):
    repo = OutboxRepository(db_session)
    published = await _add(repo)
    await _add(repo)
    await repo.mark_published(published)

    assert len(await repo.list_by_status(OutboxEventStatus.PUBLISHED)) == 1
    assert len(await repo.list_by_status(OutboxEventStatus.PENDING)) == 1


# ---------------------------------------------------------------------
# ProcessedEventRepository
# ---------------------------------------------------------------------
async def test_record_creates_a_succeeded_row(db_session):
    repo = ProcessedEventRepository(db_session)
    event_id = uuid.uuid4()

    entry, created = await repo.record(
        event_id=event_id, consumer_name="thumbnail-worker", status=ProcessedEventStatus.SUCCEEDED
    )
    assert created is True
    assert entry.status == ProcessedEventStatus.SUCCEEDED
    assert entry.error is None


async def test_has_processed_reflects_a_recorded_event(db_session):
    repo = ProcessedEventRepository(db_session)
    event_id = uuid.uuid4()

    assert await repo.has_processed(event_id, "file-worker") is False
    await repo.record(event_id=event_id, consumer_name="file-worker", status=ProcessedEventStatus.SUCCEEDED)
    assert await repo.has_processed(event_id, "file-worker") is True


async def test_duplicate_record_returns_created_false_instead_of_raising(db_session):
    """
    The losing side of an idempotency race. It must NOT surface an
    IntegrityError (which would NACK a message whose work already
    succeeded) — it must report `created=False` and hand back the
    winner's row.
    """
    repo = ProcessedEventRepository(db_session)
    event_id = uuid.uuid4()

    winner, created_first = await repo.record(
        event_id=event_id, consumer_name="file-worker", status=ProcessedEventStatus.SUCCEEDED
    )
    loser, created_second = await repo.record(
        event_id=event_id, consumer_name="file-worker", status=ProcessedEventStatus.SUCCEEDED
    )

    assert created_first is True
    assert created_second is False
    assert loser.id == winner.id


async def test_the_same_event_can_be_processed_by_two_different_consumers(db_session):
    """
    `consumer_name` is part of the key on purpose: one worker finishing
    must never make an independent worker skip its own work.
    """
    repo = ProcessedEventRepository(db_session)
    event_id = uuid.uuid4()

    _, first = await repo.record(
        event_id=event_id, consumer_name="file-worker", status=ProcessedEventStatus.SUCCEEDED
    )
    _, second = await repo.record(
        event_id=event_id, consumer_name="thumbnail-worker", status=ProcessedEventStatus.SUCCEEDED
    )

    assert first is True
    assert second is True
    assert await repo.has_processed(event_id, "notification-worker") is False


async def test_the_savepoint_keeps_the_surrounding_transaction_usable(db_session):
    """
    A duplicate INSERT must roll back only its SAVEPOINT. If it poisoned
    the outer transaction, the worker could not go on to ACK or write
    anything else.
    """
    repo = ProcessedEventRepository(db_session)
    event_id = uuid.uuid4()
    await repo.record(event_id=event_id, consumer_name="c", status=ProcessedEventStatus.SUCCEEDED)
    await repo.record(event_id=event_id, consumer_name="c", status=ProcessedEventStatus.SUCCEEDED)

    # The outer transaction is still alive and writable.
    other, created = await repo.record(
        event_id=uuid.uuid4(), consumer_name="c", status=ProcessedEventStatus.SUCCEEDED
    )
    assert created is True
    assert other.id is not None


async def test_failed_records_carry_the_error_and_are_listable(db_session):
    repo = ProcessedEventRepository(db_session)
    await repo.record(
        event_id=uuid.uuid4(),
        consumer_name="thumbnail-worker",
        status=ProcessedEventStatus.FAILED,
        error="unsupported content type: application/pdf",
    )
    await repo.record(
        event_id=uuid.uuid4(), consumer_name="thumbnail-worker", status=ProcessedEventStatus.SUCCEEDED
    )

    failures = await repo.list_failures("thumbnail-worker")
    assert len(failures) == 1
    assert "application/pdf" in failures[0].error


async def test_recorded_error_text_is_truncated(db_session):
    repo = ProcessedEventRepository(db_session)
    entry, _ = await repo.record(
        event_id=uuid.uuid4(),
        consumer_name="c",
        status=ProcessedEventStatus.FAILED,
        error="e" * 9000,
    )
    assert len(entry.error) == 2000


async def test_processed_at_is_populated(db_session):
    repo = ProcessedEventRepository(db_session)
    entry, _ = await repo.record(
        event_id=uuid.uuid4(), consumer_name="c", status=ProcessedEventStatus.SUCCEEDED
    )
    assert isinstance(entry.processed_at, datetime)
