"""
Phase 8 — outbox publisher worker tests.

Driven through `poll_once()` rather than `run()`: the loop itself is
three lines of `while not shutting_down`, while everything interesting
(what gets fetched, what gets published, what happens when Pub/Sub is
down, what happens on republish) lives in one poll. Testing the loop
would mostly be testing `asyncio.sleep`.

The worker is handed the test's own SQLite session factory, so it commits
for real — which is what makes "the row is PUBLISHED afterwards" a
genuine assertion rather than an in-memory one.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import Base
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.models.outbox_event import OutboxEvent, OutboxEventStatus
from app.repositories.outbox_repository import OutboxRepository
from app.workers.outbox_publisher import OutboxPublisherWorker
from tests.fakes.fake_pubsub import FakePublisherClient


@pytest.fixture
async def worker_db():
    """
    A file-less SQLite database with a session factory the worker can
    open its OWN sessions from — the worker is a separate process in
    production and must never share the API's request-scoped session.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


async def _seed(factory, count: int = 1, event_type: EventType = EventType.FILE_UPLOADED) -> list[uuid.UUID]:
    ids = []
    async with factory() as session:
        repo = OutboxRepository(session)
        for i in range(count):
            event = await repo.add_event(
                event_id=uuid.uuid4(),
                event_type=event_type.value,
                event_version=1,
                aggregate_type="file",
                aggregate_id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                causation_id=None,
                user_id=uuid.uuid4(),
                payload={"file_id": str(uuid.uuid4()), "index": i},
            )
            ids.append(event.event_id)
        await session.commit()
    return ids


def _worker(factory, fake: FakePublisherClient) -> OutboxPublisherWorker:
    return OutboxPublisherWorker(publisher=EventPublisher(fake, enabled=True), session_factory=factory)


async def _rows(factory) -> list[OutboxEvent]:
    async with factory() as session:
        result = await session.execute(select(OutboxEvent).order_by(OutboxEvent.created_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------
async def test_poll_publishes_pending_rows_and_marks_them_published(worker_db):
    await _seed(worker_db, 3)
    fake = FakePublisherClient()

    result = await _worker(worker_db, fake).poll_once()

    assert (result.fetched, result.published, result.failed) == (3, 3, 0)
    assert fake.total_published == 3
    for row in await _rows(worker_db):
        assert row.status == OutboxEventStatus.PUBLISHED
        assert row.published_at is not None
        assert row.last_error is None


async def test_a_published_row_is_not_republished_on_the_next_poll(worker_db):
    await _seed(worker_db, 2)
    fake = FakePublisherClient()
    worker = _worker(worker_db, fake)

    await worker.poll_once()
    second = await worker.poll_once()

    assert second.fetched == 0
    assert fake.total_published == 2


async def test_an_empty_outbox_is_a_no_op(worker_db):
    fake = FakePublisherClient()
    result = await _worker(worker_db, fake).poll_once()
    assert (result.fetched, result.published) == (0, 0)
    assert fake.publish_calls == 0


async def test_the_published_message_carries_the_stored_event_id_not_a_new_one(worker_db):
    """
    Republishing must reuse the stored `event_id` — regenerating it would
    defeat consumer-side deduplication entirely, because every redelivery
    would look like a brand-new event.
    """
    [event_id] = await _seed(worker_db, 1)
    fake = FakePublisherClient()

    await _worker(worker_db, fake).poll_once()

    (data, _attributes) = next(iter(fake.topics.values()))[0]
    assert EventEnvelope.from_json_bytes(data).event_id == event_id


async def test_the_envelope_preserves_correlation_and_payload(worker_db):
    await _seed(worker_db, 1)
    rows_before = await _rows(worker_db)
    fake = FakePublisherClient()

    await _worker(worker_db, fake).poll_once()

    (data, attributes) = next(iter(fake.topics.values()))[0]
    envelope = EventEnvelope.from_json_bytes(data)
    assert envelope.correlation_id == rows_before[0].correlation_id
    assert envelope.payload == rows_before[0].payload
    assert attributes["event_type"] == "file.uploaded"


async def test_rows_route_to_their_own_topics(worker_db):
    from app.core.config import get_settings

    await _seed(worker_db, 1, EventType.FILE_UPLOADED)
    await _seed(worker_db, 1, EventType.UPLOAD_COMPLETED)
    await _seed(worker_db, 1, EventType.NOTIFICATION_REQUESTED)

    fake = FakePublisherClient()
    await _worker(worker_db, fake).poll_once()

    settings = get_settings()
    assert len(fake.topics) == 3
    assert len(fake.messages_on(fake.topic_path(settings.GCP_PROJECT_ID, settings.UPLOAD_EVENTS_TOPIC))) == 1


# ---------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------
async def test_a_publish_failure_marks_the_row_failed_with_backoff_and_keeps_it_retryable(worker_db):
    await _seed(worker_db, 1)
    fake = FakePublisherClient()
    fake.start_failing()

    result = await _worker(worker_db, fake).poll_once()

    assert (result.published, result.failed) == (0, 1)
    [row] = await _rows(worker_db)
    assert row.status == OutboxEventStatus.FAILED
    assert row.attempt_count == 1
    assert row.last_error
    # FAILED is NOT terminal — the row is still eligible once backoff elapses.
    assert row.published_at is None


async def test_one_failing_row_does_not_prevent_its_siblings_from_publishing(worker_db):
    """
    This is why the commit is per row, not per batch: a batched commit
    would roll back the successes alongside the failure.
    """
    await _seed(worker_db, 3)
    fake = FakePublisherClient()
    fake.start_failing(after=2)  # rows 1 and 2 succeed, row 3 fails

    result = await _worker(worker_db, fake).poll_once()

    assert (result.published, result.failed) == (2, 1)
    statuses = sorted(r.status.value for r in await _rows(worker_db))
    assert statuses == ["failed", "published", "published"]


async def test_a_failed_row_publishes_once_the_transport_recovers(worker_db):
    await _seed(worker_db, 1)
    fake = FakePublisherClient()
    worker = _worker(worker_db, fake)

    fake.start_failing()
    await worker.poll_once()

    fake.stop_failing()
    # Wind the backoff back, as the passage of time would.
    async with worker_db() as session:
        repo = OutboxRepository(session)
        [row] = await repo.list_by_status(OutboxEventStatus.FAILED)
        from datetime import datetime, timedelta, timezone

        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    result = await worker.poll_once()
    assert result.published == 1
    assert (await _rows(worker_db))[0].status == OutboxEventStatus.PUBLISHED


async def test_backoff_keeps_a_failed_row_out_of_the_very_next_poll(worker_db):
    """Without this, a Pub/Sub outage becomes a self-inflicted retry storm."""
    await _seed(worker_db, 1)
    fake = FakePublisherClient()
    fake.start_failing()
    worker = _worker(worker_db, fake)

    await worker.poll_once()
    second = await worker.poll_once()

    assert second.fetched == 0  # still backing off


async def test_an_unmappable_event_type_fails_that_row_only_and_never_crashes_the_loop(worker_db):
    """A row with a bogus event_type must not be able to stop the publisher."""
    async with worker_db() as session:
        repo = OutboxRepository(session)
        await repo.add_event(
            event_id=uuid.uuid4(),
            event_type="file.teleported",  # not in the catalog
            event_version=1,
            aggregate_type="file",
            aggregate_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            causation_id=None,
            user_id=uuid.uuid4(),
            payload={},
        )
        await session.commit()
    await _seed(worker_db, 1)  # one good row alongside it

    fake = FakePublisherClient()
    result = await _worker(worker_db, fake).poll_once()

    assert result.failed == 1
    assert result.published == 1


# ---------------------------------------------------------------------
# Kill switch & shutdown
# ---------------------------------------------------------------------
async def test_with_pubsub_disabled_rows_are_still_drained_as_no_ops(worker_db):
    """
    The kill switch means "stop talking to Pub/Sub", not "stop the
    publisher". Rows are marked PUBLISHED via the documented no-op path so
    the outbox does not grow unboundedly while the switch is off.
    """
    await _seed(worker_db, 2)
    fake = FakePublisherClient()
    worker = OutboxPublisherWorker(
        publisher=EventPublisher(fake, enabled=False), session_factory=worker_db
    )

    result = await worker.poll_once()

    assert result.published == 2
    assert fake.publish_calls == 0
    assert all(r.status == OutboxEventStatus.PUBLISHED for r in await _rows(worker_db))


async def test_shutdown_stops_the_batch_partway_leaving_the_rest_pending(worker_db):
    await _seed(worker_db, 5)
    fake = FakePublisherClient()
    worker = _worker(worker_db, fake)
    worker._init_runtime()
    worker.request_shutdown()

    result = await worker.poll_once()

    assert result.published == 0  # stopped before the first row
    assert all(r.status == OutboxEventStatus.PENDING for r in await _rows(worker_db))


async def test_request_shutdown_is_idempotent(worker_db):
    worker = _worker(worker_db, FakePublisherClient())
    worker._init_runtime()
    worker.request_shutdown()
    worker.request_shutdown()
    assert worker.shutting_down is True


async def test_heartbeat_file_is_written(tmp_path, worker_db, monkeypatch):
    """
    Liveness is a timer-driven file touch, independent of message
    arrival — an idle worker is healthy, not dead.
    """
    worker = _worker(worker_db, FakePublisherClient())
    worker._init_runtime()
    heartbeat = tmp_path / "healthy"
    worker._heartbeat_path = str(heartbeat)

    worker.touch_heartbeat()
    assert heartbeat.exists()


async def test_heartbeat_write_failure_does_not_raise(worker_db):
    """A read-only/full filesystem must not crash a worker that is otherwise fine."""
    worker = _worker(worker_db, FakePublisherClient())
    worker._init_runtime()
    worker._heartbeat_path = "\x00invalid/path/healthy"
    worker.touch_heartbeat()  # must not raise
