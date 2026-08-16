"""
Phase 8 — `BaseWorker` tests, via a trivial subclass.

Everything here is about the ack/nack decision table in
`app/workers/base.py`'s docstring, because that table is where events get
silently lost or infinitely redelivered if it is wrong. A trivial
`process()` (record the call, or raise on demand) isolates the skeleton
from any real worker's business logic.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import Base
from app.events.envelope import EventEnvelope, EventType
from app.exceptions.custom_exceptions import NonRetryableEventError, RetryableEventError
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.repositories.processed_event_repository import ProcessedEventRepository
from app.workers.base import BaseWorker
from tests.fakes.fake_pubsub import FakeMessage


@pytest.fixture
async def worker_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


class SpyWorker(BaseWorker):
    worker_name = "spy-worker"
    consumer_name = "spy-worker"

    def __init__(self, *, raises=None, only_types=None, **kwargs):
        self.calls: list[EventEnvelope] = []
        self._raises = raises
        self._only_types = only_types
        super().__init__(subscription="spy-sub", **kwargs)

    def default_subscription(self) -> str:
        return "spy-sub"

    def interested_in(self, envelope):
        return self._only_types is None or envelope.event_type in self._only_types

    async def process(self, envelope, session):
        self.calls.append(envelope)
        if self._raises is not None:
            raise self._raises


def _message(event_type: EventType = EventType.FILE_UPLOADED, **overrides) -> tuple[FakeMessage, EventEnvelope]:
    envelope = EventEnvelope(
        event_type=event_type, user_id=uuid.uuid4(), payload={"file_id": str(uuid.uuid4())}, **overrides
    )
    data, attributes = envelope.to_pubsub_message()
    return FakeMessage(data, attributes), envelope


async def _processed(factory, consumer="spy-worker") -> list[ProcessedEvent]:
    async with factory() as session:
        result = await session.execute(
            select(ProcessedEvent).where(ProcessedEvent.consumer_name == consumer)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------
async def test_a_successful_message_is_acked_and_recorded(worker_db):
    worker = SpyWorker(session_factory=worker_db)
    message, envelope = _message()

    await worker._handle(message)

    assert message.acked is True
    assert message.nacked is False
    assert len(worker.calls) == 1
    [row] = await _processed(worker_db)
    assert row.event_id == envelope.event_id
    assert row.status == ProcessedEventStatus.SUCCEEDED
    assert row.error is None


async def test_every_path_settles_the_message_exactly_once(worker_db):
    """Neither acked nor nacked means the message stalls until the ack deadline expires."""
    for raises in (None, NonRetryableEventError("nope"), RetryableEventError("later")):
        worker = SpyWorker(session_factory=worker_db, raises=raises)
        message, _ = _message()
        await worker._handle(message)
        assert message.settled, f"unsettled for raises={raises!r}"


async def test_the_envelope_reaches_process_intact(worker_db):
    worker = SpyWorker(session_factory=worker_db)
    message, envelope = _message()

    await worker._handle(message)

    received = worker.calls[0]
    assert received.event_id == envelope.event_id
    assert received.correlation_id == envelope.correlation_id
    assert received.payload == envelope.payload


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------
async def test_a_redelivered_message_is_acked_without_reprocessing(worker_db):
    """
    Pub/Sub is at-least-once by design; so is the outbox. Redelivery is
    the expected case, not an edge case.
    """
    worker = SpyWorker(session_factory=worker_db)
    message, envelope = _message()

    await worker._handle(message)
    # The SAME logical event, redelivered with a fresh Pub/Sub message id.
    redelivery = FakeMessage(envelope.to_json_bytes(), message_id="different-id", delivery_attempt=2)
    await worker._handle(redelivery)

    assert len(worker.calls) == 1  # process() ran once
    assert redelivery.acked is True
    assert len(await _processed(worker_db)) == 1


async def test_two_consumers_both_process_the_same_event(worker_db):
    """`consumer_name` is part of the idempotency key for exactly this reason."""

    class OtherWorker(SpyWorker):
        worker_name = "other-worker"
        consumer_name = "other-worker"

    first = SpyWorker(session_factory=worker_db)
    second = OtherWorker(session_factory=worker_db)
    _msg, envelope = _message()

    await first._handle(FakeMessage(envelope.to_json_bytes()))
    await second._handle(FakeMessage(envelope.to_json_bytes()))

    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert len(await _processed(worker_db, "spy-worker")) == 1
    assert len(await _processed(worker_db, "other-worker")) == 1


async def test_a_losing_idempotency_race_still_acks(worker_db):
    """
    The winner's equivalent work already succeeded, so NACKing would ask
    Pub/Sub to redeliver work that is definitionally already done.
    """
    worker = SpyWorker(session_factory=worker_db)
    _msg, envelope = _message()

    # Simulate the other replica having already recorded the outcome
    # AFTER our pre-check would have passed, by writing it directly.
    async with worker_db() as session:
        await ProcessedEventRepository(session).record(
            event_id=envelope.event_id,
            consumer_name="spy-worker",
            status=ProcessedEventStatus.SUCCEEDED,
        )
        await session.commit()

    message = FakeMessage(envelope.to_json_bytes())
    await worker._handle(message)

    assert message.acked is True
    assert message.nacked is False


# ---------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------
async def test_a_non_retryable_error_acks_and_records_a_failed_row_with_the_reason(worker_db):
    worker = SpyWorker(session_factory=worker_db, raises=NonRetryableEventError("unsupported content type"))
    message, _ = _message()

    await worker._handle(message)

    assert message.acked is True
    assert message.nacked is False
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "unsupported content type" in row.error


async def test_a_retryable_error_nacks_and_records_nothing(worker_db):
    """
    Recording a retryable failure would record an outcome that has not
    happened yet — and would then make the retry look like a duplicate.
    """
    worker = SpyWorker(session_factory=worker_db, raises=RetryableEventError("GCS timed out"))
    message, _ = _message()

    await worker._handle(message)

    assert message.nacked is True
    assert message.acked is False
    assert await _processed(worker_db) == []


async def test_an_unexpected_exception_is_treated_as_retryable(worker_db):
    """'Try again' is the safe default when we do not know what broke."""
    worker = SpyWorker(session_factory=worker_db, raises=RuntimeError("something nobody anticipated"))
    message, _ = _message()

    await worker._handle(message)

    assert message.nacked is True
    assert await _processed(worker_db) == []


async def test_a_nacked_message_is_reprocessed_on_redelivery(worker_db):
    """The retry must actually run the work, not be swallowed as a duplicate."""

    class FlakyWorker(SpyWorker):
        def __init__(self, **kwargs):
            self.attempts = 0
            super().__init__(**kwargs)

        async def process(self, envelope, session):
            self.attempts += 1
            if self.attempts == 1:
                raise RetryableEventError("transient")

    worker = FlakyWorker(session_factory=worker_db)
    _msg, envelope = _message()

    first = FakeMessage(envelope.to_json_bytes())
    await worker._handle(first)
    assert first.nacked is True

    second = FakeMessage(envelope.to_json_bytes(), delivery_attempt=2)
    await worker._handle(second)
    assert second.acked is True
    assert worker.attempts == 2
    assert len(await _processed(worker_db)) == 1


# ---------------------------------------------------------------------
# Malformed input & filtering
# ---------------------------------------------------------------------
async def test_unparseable_bytes_are_acked_and_discarded(worker_db):
    """Redelivering the same garbage produces the same garbage forever."""
    worker = SpyWorker(session_factory=worker_db)
    message = FakeMessage(b"this is definitely not an envelope")

    await worker._handle(message)

    assert message.acked is True
    assert message.nacked is False
    assert worker.calls == []
    assert await _processed(worker_db) == []


async def test_an_unknown_event_type_in_otherwise_valid_json_is_discarded(worker_db):
    worker = SpyWorker(session_factory=worker_db)
    message = FakeMessage(b'{"event_type": "file.teleported", "user_id": "not-even-a-uuid"}')

    await worker._handle(message)

    assert message.acked is True
    assert worker.calls == []


async def test_an_uninteresting_event_is_acked_without_a_processed_row(worker_db):
    """
    Declining is not processing: recording it would pollute the ledger
    that answers "did this consumer handle this event?".
    """
    worker = SpyWorker(session_factory=worker_db, only_types={EventType.THUMBNAIL_REQUESTED})
    message, _ = _message(EventType.FILE_UPLOADED)

    await worker._handle(message)

    assert message.acked is True
    assert worker.calls == []
    assert await _processed(worker_db) == []


async def test_an_interesting_event_still_gets_through_the_filter(worker_db):
    worker = SpyWorker(session_factory=worker_db, only_types={EventType.THUMBNAIL_REQUESTED})
    message, _ = _message(EventType.THUMBNAIL_REQUESTED)

    await worker._handle(message)

    assert len(worker.calls) == 1
    assert message.acked is True


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------
async def test_shutdown_cancels_the_pull_and_closes_the_subscriber(worker_db):
    from tests.fakes.fake_pubsub import FakeSubscriberClient

    subscriber = FakeSubscriberClient()
    worker = SpyWorker(session_factory=worker_db, subscriber_client=subscriber)
    worker._streaming_future = subscriber.subscribe("projects/p/subscriptions/s", worker._sync_callback)

    await worker.shutdown()

    assert subscriber.cancelled is True
    assert subscriber.closed is True


async def test_worker_name_and_consumer_name_are_separate_concepts(worker_db):
    """
    Renaming a deployment must never reset the idempotency ledger, which
    would make every historical event look unprocessed.
    """
    worker = SpyWorker(session_factory=worker_db)
    assert hasattr(worker, "worker_name")
    assert hasattr(worker, "consumer_name")
    _msg, envelope = _message()
    await worker._handle(FakeMessage(envelope.to_json_bytes()))
    [row] = await _processed(worker_db)
    assert row.consumer_name == SpyWorker.consumer_name
