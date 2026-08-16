"""
Phase 8 — file processing worker tests.

Driven through `_handle()` with `FakeMessage`, `FakeGCSClient` and
`FakePublisherClient`, so the ack/nack decision and the fan-out are both
real assertions. The most load-bearing test in this file is
`test_derived_event_ids_are_deterministic...`: a random derived id would
silently defeat downstream deduplication on every retry, and nothing
downstream would ever notice.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import Base
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.services.storage_service import StorageService
from app.workers.file_processing_worker import FileProcessingWorker, derive_event_id
from tests.fakes.fake_gcs import FakeGCSClient
from tests.fakes.fake_pubsub import FakeMessage, FakePublisherClient


@pytest.fixture
async def worker_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


def _storage_with_object(object_name: str, data: bytes = b"pretend image bytes", content_type="image/png"):
    import io

    from app.core.config import get_settings

    client = FakeGCSClient()
    storage = StorageService(client)
    blob = client.bucket(get_settings().GCS_BUCKET_NAME).blob(object_name)
    blob.upload_from_file(io.BytesIO(data), content_type=content_type, size=len(data))
    return storage


def _event(
    *,
    object_name="default/u/2026/08/abc.png",
    content_type="image/png",
    size=19,
    event_type=EventType.FILE_UPLOADED,
    file_id=None,
    **payload_overrides,
) -> EventEnvelope:
    payload = {
        "file_id": file_id or str(uuid.uuid4()),
        "filename": "photo.png",
        "content_type": content_type,
        "size": size,
        "bucket_name": "nimbusfs-files-dev",
        "object_name": object_name,
    }
    payload.update(payload_overrides)
    return EventEnvelope(event_type=event_type, user_id=uuid.uuid4(), payload=payload)


def _worker(worker_db, storage, fake_pubsub) -> FileProcessingWorker:
    return FileProcessingWorker(
        storage_service=storage,
        publisher=EventPublisher(fake_pubsub, enabled=True),
        session_factory=worker_db,
        subscription="file-sub",
    )


async def _processed(factory) -> list[ProcessedEvent]:
    async with factory() as session:
        result = await session.execute(select(ProcessedEvent))
        return list(result.scalars().all())


# ---------------------------------------------------------------------
# Happy path & fan-out
# ---------------------------------------------------------------------
async def test_an_image_upload_fans_out_to_thumbnail_and_notification(worker_db):
    envelope = _event()
    storage = _storage_with_object(envelope.payload["object_name"])
    fake = FakePublisherClient()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, storage, fake)._handle(message)

    assert message.acked is True
    assert sorted(fake.published_event_types()) == ["notification.requested", "thumbnail.requested"]
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.SUCCEEDED


async def test_a_non_image_upload_fans_out_to_notification_only(worker_db):
    """Most files are not images. That is normal, not a failure."""
    envelope = _event(content_type="application/pdf", object_name="default/u/2026/08/doc.pdf")
    storage = _storage_with_object(envelope.payload["object_name"], content_type="application/pdf")
    fake = FakePublisherClient()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, storage, fake)._handle(message)

    assert message.acked is True
    assert fake.published_event_types() == ["notification.requested"]


async def test_both_upload_paths_are_handled_by_the_same_consumer(worker_db):
    """
    Phase 3 emits `file.uploaded`, Phase 6 emits `file.completed`.
    Downstream should need one code path, not one per upload mechanism.
    """
    for event_type in (EventType.FILE_UPLOADED, EventType.FILE_COMPLETED):
        envelope = _event(event_type=event_type)
        storage = _storage_with_object(envelope.payload["object_name"])
        fake = FakePublisherClient()
        message = FakeMessage(envelope.to_json_bytes())

        await _worker(worker_db, storage, fake)._handle(message)

        assert message.acked is True, event_type
        assert "notification.requested" in fake.published_event_types()


async def test_unrelated_file_events_on_the_shared_topic_are_declined(worker_db):
    """FILE_EVENTS_TOPIC also carries folder/rename/move/thumbnail events."""
    envelope = _event(event_type=EventType.FILE_RENAMED)
    fake = FakePublisherClient()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, _storage_with_object("x"), fake)._handle(message)

    assert message.acked is True
    assert fake.total_published == 0
    assert await _processed(worker_db) == []  # declining is not processing


async def test_the_derived_events_carry_causation_and_the_same_correlation(worker_db):
    envelope = _event()
    storage = _storage_with_object(envelope.payload["object_name"])
    fake = FakePublisherClient()

    await _worker(worker_db, storage, fake)._handle(FakeMessage(envelope.to_json_bytes()))

    children = [
        EventEnvelope.from_json_bytes(data)
        for messages in fake.topics.values()
        for data, _ in messages
    ]
    assert children
    for child in children:
        assert child.correlation_id == envelope.correlation_id  # same user operation
        assert child.causation_id == envelope.event_id  # this event caused it
        assert child.producer == "file-processing-worker"


async def test_derived_event_ids_are_deterministic_so_a_retry_does_not_defeat_deduplication(worker_db):
    """
    THE subtle bug this design is easiest to lose to: a random derived
    `event_id` would make every retry of the fan-out look like a
    brand-new event downstream, so the thumbnail worker would regenerate
    a thumbnail on every redelivery forever.
    """
    envelope = _event()
    storage = _storage_with_object(envelope.payload["object_name"])

    first = FakePublisherClient()
    await _worker(worker_db, storage, first)._handle(FakeMessage(envelope.to_json_bytes()))

    # A completely fresh worker + fresh database: a genuine redelivery.
    second = FakePublisherClient()
    engine_free_worker = FileProcessingWorker(
        storage_service=storage,
        publisher=EventPublisher(second, enabled=True),
        session_factory=worker_db,
        subscription="file-sub",
    )
    engine_free_worker.consumer_name = "file-processing-worker-replica-2"
    await engine_free_worker._handle(FakeMessage(envelope.to_json_bytes()))

    def ids(fake):
        return sorted(
            str(EventEnvelope.from_json_bytes(d).event_id)
            for messages in fake.topics.values()
            for d, _ in messages
        )

    assert ids(first) == ids(second)
    assert str(derive_event_id(envelope.event_id, EventType.THUMBNAIL_REQUESTED)) in ids(first)


def test_derive_event_id_is_stable_and_distinct_per_child_type():
    parent = uuid.uuid4()
    assert derive_event_id(parent, EventType.THUMBNAIL_REQUESTED) == derive_event_id(
        parent, EventType.THUMBNAIL_REQUESTED
    )
    assert derive_event_id(parent, EventType.THUMBNAIL_REQUESTED) != derive_event_id(
        parent, EventType.NOTIFICATION_REQUESTED
    )
    assert derive_event_id(parent, EventType.THUMBNAIL_REQUESTED) != derive_event_id(
        uuid.uuid4(), EventType.THUMBNAIL_REQUESTED
    )


# ---------------------------------------------------------------------
# Verification failures
# ---------------------------------------------------------------------
async def test_a_missing_storage_object_is_permanent_and_acks_with_a_failed_row(worker_db):
    """
    The bytes are gone. Redelivering will not bring them back — so ACK,
    and leave a queryable FAILED row rather than DLQ noise.
    """
    envelope = _event(object_name="default/u/2026/08/vanished.png")
    storage = _storage_with_object("some/other/object.png")
    fake = FakePublisherClient()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, storage, fake)._handle(message)

    assert message.acked is True
    assert message.nacked is False
    assert fake.total_published == 0  # no fan-out for a file that has no bytes
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED
    assert "does not exist" in row.error


async def test_a_payload_missing_required_fields_is_permanent(worker_db):
    """The producer is this same codebase — a missing field is a bug, not a blip."""
    envelope = EventEnvelope(
        event_type=EventType.FILE_UPLOADED, user_id=uuid.uuid4(), payload={"filename": "x.png"}
    )
    message = FakeMessage(envelope.to_json_bytes())
    fake = FakePublisherClient()

    await _worker(worker_db, _storage_with_object("x"), fake)._handle(message)

    assert message.acked is True
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.FAILED


async def test_a_transient_storage_error_nacks_for_redelivery(worker_db):
    class ExplodingStorage:
        async def get_blob_metadata(self, object_name):
            raise TimeoutError("GCS timed out")

    envelope = _event()
    fake = FakePublisherClient()
    worker = _worker(worker_db, ExplodingStorage(), fake)
    message = FakeMessage(envelope.to_json_bytes())

    await worker._handle(message)

    assert message.nacked is True
    assert message.acked is False
    assert await _processed(worker_db) == []  # nothing recorded for a retryable failure


async def test_a_size_mismatch_is_logged_but_does_not_fail_the_event(worker_db):
    """
    A metadata/bytes disagreement is evidence for a human. Rewriting
    either side to agree would destroy it; failing the event would just
    hide it behind a retry counter.
    """
    envelope = _event(size=999999)  # claims a size the object does not have
    storage = _storage_with_object(envelope.payload["object_name"])
    fake = FakePublisherClient()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, storage, fake)._handle(message)

    assert message.acked is True
    [row] = await _processed(worker_db)
    assert row.status == ProcessedEventStatus.SUCCEEDED


async def test_a_fan_out_publish_failure_nacks_so_the_whole_step_is_retried(worker_db):
    envelope = _event()
    storage = _storage_with_object(envelope.payload["object_name"])
    fake = FakePublisherClient()
    fake.start_failing()
    message = FakeMessage(envelope.to_json_bytes())

    await _worker(worker_db, storage, fake)._handle(message)

    assert message.nacked is True
    assert await _processed(worker_db) == []


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------
async def test_a_redelivered_upload_event_does_not_fan_out_twice(worker_db):
    envelope = _event()
    storage = _storage_with_object(envelope.payload["object_name"])
    fake = FakePublisherClient()
    worker = _worker(worker_db, storage, fake)

    await worker._handle(FakeMessage(envelope.to_json_bytes()))
    await worker._handle(FakeMessage(envelope.to_json_bytes(), delivery_attempt=2))

    assert fake.total_published == 2  # one fan-out, not two
    assert len(await _processed(worker_db)) == 1
