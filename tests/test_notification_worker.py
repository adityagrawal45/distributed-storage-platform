"""
Phase 8 — notification service + worker tests.

Driven through `_handle()` with `FakeMessage`, exactly like the thumbnail
and file-processing worker tests, so the ack/nack decision is a real
assertion rather than an inspection of `process()` in isolation.

The interesting assertions here are not "a row was written" — they are
that the row is written in the SAME transaction as the `ProcessedEvent`
that records it (so a duplicate delivery cannot notify twice), and that a
payload this codebase's own producer should never have emitted is treated
as permanent rather than retried forever.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import Base
from app.events.envelope import EventEnvelope, EventType
from app.exceptions.custom_exceptions import NonRetryableEventError
from app.models.notification import Notification
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.services.notification_service import (
    LoggingNotificationSender,
    NotificationSender,
    render_notification,
)
from app.workers.notification_worker import NotificationWorker
from tests.fakes.fake_pubsub import FakeMessage


@pytest.fixture
async def worker_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


def _request(
    *,
    notification_type: str = "file_ready",
    file_id: str | None = None,
    filename: str | None = "photo.png",
    user_id: uuid.UUID | None = None,
    event_type: EventType = EventType.NOTIFICATION_REQUESTED,
    **payload_overrides,
) -> EventEnvelope:
    payload = {
        "notification_type": notification_type,
        "file_id": file_id if file_id is not None else str(uuid.uuid4()),
        "filename": filename,
        "size": 1234,
    }
    payload.update(payload_overrides)
    return EventEnvelope(
        event_type=event_type,
        producer="file-processing-worker",
        user_id=user_id or uuid.uuid4(),
        payload=payload,
    )


def _worker(worker_db, **kwargs) -> NotificationWorker:
    return NotificationWorker(session_factory=worker_db, subscription="notify-sub", **kwargs)


async def _processed(factory) -> list[ProcessedEvent]:
    async with factory() as session:
        result = await session.execute(select(ProcessedEvent))
        return list(result.scalars().all())


async def _notifications(factory) -> list[Notification]:
    async with factory() as session:
        result = await session.execute(select(Notification))
        return list(result.scalars().all())


# ---------------------------------------------------------------------
# render_notification — the payload contract
# ---------------------------------------------------------------------
def test_a_known_type_renders_its_template():
    notification = render_notification(_request(filename="holiday.jpg"))

    assert notification.notification_type == "file_ready"
    assert "holiday.jpg" in notification.subject
    assert "holiday.jpg" in notification.body


def test_an_unknown_type_falls_back_instead_of_failing():
    """
    A producer introducing a new notification type before consumers know
    it is an ADDITIVE change the envelope's versioning contract permits.
    Dropping the notification — or crash-looping on it — would be a worse
    answer than a generic one.
    """
    notification = render_notification(_request(notification_type="quota_warning"))

    assert notification.notification_type == "quota_warning"
    assert notification.subject == "NimbusFS update"
    assert "quota_warning" in notification.body


def test_a_missing_filename_does_not_break_the_template():
    notification = render_notification(_request(filename=None))

    assert "your file" in notification.subject


def test_the_recipient_comes_from_the_envelope_not_the_payload():
    """
    `user_id` is an envelope field precisely so a handler cannot get the
    recipient wrong by trusting a payload key that may not be there.
    """
    user_id = uuid.uuid4()
    notification = render_notification(_request(user_id=user_id))

    assert notification.user_id == user_id


def test_a_missing_notification_type_is_permanent():
    with pytest.raises(NonRetryableEventError):
        render_notification(_request(notification_type=""))


def test_a_malformed_file_id_is_permanent():
    with pytest.raises(NonRetryableEventError):
        render_notification(_request(file_id="not-a-uuid"))


def test_an_absent_file_id_is_allowed():
    """Not every notification is about a file — `related_file_id` is nullable."""
    notification = render_notification(_request(file_id="", notification_type="quota_warning"))

    assert notification.related_file_id is None


def test_a_long_subject_is_truncated_to_the_column_width():
    notification = render_notification(_request(filename="x" * 500))

    assert len(notification.subject) <= 255


# ---------------------------------------------------------------------
# LoggingNotificationSender
# ---------------------------------------------------------------------
async def test_the_sender_writes_a_row_but_does_not_commit(worker_db):
    """
    The commit point belongs to `BaseWorker`, so the notification and its
    `ProcessedEvent` land atomically. A sender that committed on its own
    would let a crash between the two produce a delivered notification
    with no ledger entry — and therefore a duplicate on redelivery.
    """
    notification = render_notification(_request())

    async with worker_db() as session:
        await LoggingNotificationSender(session).send(notification)
        # Visible inside the transaction...
        result = await session.execute(select(Notification))
        assert len(list(result.scalars().all())) == 1
        await session.rollback()

    # ...and gone once it is rolled back, proving nothing was committed.
    assert await _notifications(worker_db) == []


# ---------------------------------------------------------------------
# NotificationWorker — success path
# ---------------------------------------------------------------------
async def test_the_worker_writes_a_notification_row_and_acks(worker_db):
    user_id = uuid.uuid4()
    file_id = uuid.uuid4()
    envelope = _request(user_id=user_id, file_id=str(file_id), filename="report.pdf")
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db)._handle(message)

    assert message.acked is True
    assert message.nacked is False
    [notification] = await _notifications(worker_db)
    assert notification.user_id == user_id
    assert notification.related_file_id == file_id
    assert notification.notification_type == "file_ready"
    assert "report.pdf" in notification.subject
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.SUCCEEDED
    assert row.consumer_name == "notification-worker"


# ---------------------------------------------------------------------
# NotificationWorker — duplicate delivery
# ---------------------------------------------------------------------
async def test_a_redelivered_notification_is_not_sent_twice(worker_db):
    """Pub/Sub is at-least-once; a user must not get two emails for one upload."""
    envelope = _request()
    worker = _worker(worker_db)

    await worker._handle(FakeMessage(envelope.to_json_bytes()))
    second = FakeMessage(envelope.to_json_bytes(), delivery_attempt=2)
    await worker._handle(second)

    assert second.acked is True
    assert len(await _notifications(worker_db)) == 1
    assert len(await _processed(worker_db)) == 1


async def test_a_different_consumer_is_not_blocked_by_this_ones_ledger(worker_db):
    """
    Idempotency is keyed on (event_id, consumer_name), not event_id
    alone — otherwise adding a second consumer to a topic would find
    every historical event already "processed".
    """
    envelope = _request()
    await _worker(worker_db)._handle(FakeMessage(envelope.to_json_bytes()))

    other = _worker(worker_db)
    other.consumer_name = "notification-worker-v2"
    await other._handle(FakeMessage(envelope.to_json_bytes()))

    assert len(await _notifications(worker_db)) == 2
    assert len(await _processed(worker_db)) == 2


# ---------------------------------------------------------------------
# NotificationWorker — failure paths
# ---------------------------------------------------------------------
async def test_an_invalid_payload_acks_with_a_failed_row_and_no_notification(worker_db):
    message = FakeMessage(_request(notification_type="").to_json_bytes())

    await _worker(worker_db)._handle(message)

    assert message.acked is True
    assert message.nacked is False
    assert await _notifications(worker_db) == []
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "notification_type" in row.error


async def test_unparseable_bytes_are_acked_without_a_ledger_row(worker_db):
    """No event_id to key a ProcessedEvent on — and redelivering the same bytes cannot help."""
    message = FakeMessage(b"{not json at all")

    await _worker(worker_db)._handle(message)

    assert message.acked is True
    assert await _processed(worker_db) == []


async def test_the_worker_declines_events_it_does_not_own(worker_db):
    message = FakeMessage(_request(event_type=EventType.FILE_UPLOADED).to_json_bytes())

    await _worker(worker_db)._handle(message)

    assert message.acked is True
    assert await _notifications(worker_db) == []
    assert await _processed(worker_db) == []  # declining is not processing


async def test_a_transient_sender_failure_nacks_and_records_nothing(worker_db):
    """
    A down email provider is the archetypal retryable failure: NACK, let
    Pub/Sub redeliver, and leave no ledger row claiming success.
    """

    class FlakySender(NotificationSender):
        def __init__(self, session):
            self._session = session

        async def send(self, notification):
            raise TimeoutError("notification provider timed out")

    message = FakeMessage(_request().to_json_bytes())

    await _worker(worker_db, sender_factory=FlakySender)._handle(message)

    assert message.nacked is True
    assert message.acked is False
    assert await _notifications(worker_db) == []
    assert await _processed(worker_db) == []


async def test_a_permanently_undeliverable_notification_acks_with_a_failed_row(worker_db):
    class RejectingSender(NotificationSender):
        def __init__(self, session):
            self._session = session

        async def send(self, notification):
            raise NonRetryableEventError("Recipient address is permanently rejected.")

    message = FakeMessage(_request().to_json_bytes())

    await _worker(worker_db, sender_factory=RejectingSender)._handle(message)

    assert message.acked is True
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "permanently rejected" in row.error
