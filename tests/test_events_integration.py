"""
Phase 8 — the whole event chain, end to end, through the real components.

What this file is for
---------------------
Every other Phase 8 test file proves one component in isolation. This one
proves they **compose**: that the payload the upload service writes into
an outbox row is the payload the outbox publisher puts on the wire, that
the bytes the file-processing worker reads off that wire parse back into
the envelope it expects, that the derived event it publishes is one the
thumbnail worker recognizes, and that the file id threaded through all of
it still resolves to a real row at the end. Contract drift between two
adjacent components is invisible to unit tests of either one; it is only
visible here.

Nothing is mocked between the stages. The chain runs:

    POST /files/upload  (real FastAPI + real FileUploadService)
        -> OutboxEvent row, PENDING, in the request's own transaction
    OutboxPublisherWorker.poll_once()
        -> FakePublisherClient now holds the message; row is PUBLISHED
    FileProcessingWorker._handle(FakeMessage(those exact bytes))
        -> ProcessedEvent(SUCCEEDED) + thumbnail.requested
                                     + notification.requested published
    ThumbnailWorker._handle(the thumbnail message)
        -> a real PNG in FakeGCSClient + FileMetadata.thumbnail_object_name
    NotificationWorker._handle(the notification message)
        -> a Notification row

What is deliberately NOT simulated
----------------------------------
Pub/Sub's *server* behavior: no ack deadlines, no automatic redelivery,
no dead-letter routing, no delivery ordering. Those are Google's
semantics, and faking them would be asserting our guesses about them
rather than our code. Where redelivery matters, the test hands the same
bytes to the worker a second time explicitly — a stronger test than a
probabilistic one, because the duplicate is guaranteed.

Why the workers share the test's ONE session
--------------------------------------------
`_SharedSessionFactory` below hands every worker the same `db_session`
the HTTP client wrote through. In production each worker owns its own
session against its own connection; here that would mean a second
connection that cannot see the uncommitted rows the request transaction
holds. Sharing the session is the honest way to model "the same database"
in a fixture whose whole design is one in-memory transaction — and it
does not weaken any assertion in this file, because nothing here is
testing transaction isolation between processes (the outbox pattern's
atomicity guarantee is tested in `test_event_emission.py`, where it
belongs).
"""

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.models.file_metadata import FileMetadata
from app.models.notification import Notification
from app.models.outbox_event import OutboxEvent, OutboxEventStatus
from app.models.processed_event import ProcessedEvent, ProcessedEventStatus
from app.services.storage_service import StorageService
from app.workers.file_processing_worker import FileProcessingWorker
from app.workers.notification_worker import NotificationWorker
from app.workers.outbox_publisher import OutboxPublisherWorker
from app.workers.thumbnail_worker import ThumbnailWorker
from tests.fakes.fake_pubsub import FakeMessage

SETTINGS = get_settings()


# ---------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------
class _SharedSessionFactory:
    """
    An `async_sessionmaker`-shaped object that always yields the SAME
    session and never closes it — see the module docstring.

    Workers use it as `async with self._session_factory() as session:`,
    so it has to be callable *and* an async context manager.
    """

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


def _topic_path(topic_name: str) -> str:
    return f"projects/{SETTINGS.GCP_PROJECT_ID}/topics/{topic_name}"


def _published(fake, topic_name: str, event_type: EventType) -> list[EventEnvelope]:
    """
    Every envelope of `event_type` currently sitting on `topic_name`.

    Filtered on the Pub/Sub *attribute* rather than by parsing every
    message, because that is exactly how a real subscription filter would
    select them — which is the reason `to_pubsub_message()` duplicates
    `event_type` into the attributes in the first place.
    """
    return [
        EventEnvelope.from_json_bytes(data)
        for data, attributes in fake.messages_on(_topic_path(topic_name))
        if attributes.get("event_type") == event_type.value
    ]


def _png_bytes(size=(900, 600)) -> bytes:
    """A genuine PNG — the thumbnail worker really decodes this."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(30, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _upload(authed_client, *, filename: str, content: bytes, content_type: str) -> dict:
    response = await authed_client.post(
        "/api/v1/files/upload",
        files={"file": (filename, io.BytesIO(content), content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["file"]


async def _outbox_rows(db_session) -> list[OutboxEvent]:
    result = await db_session.execute(select(OutboxEvent).order_by(OutboxEvent.created_at))
    return list(result.scalars().all())


async def _processed_rows(db_session) -> list[ProcessedEvent]:
    result = await db_session.execute(select(ProcessedEvent))
    return list(result.scalars().all())


@pytest.fixture
def chain(db_session, fake_gcs_client, fake_pubsub_client):
    """
    All four workers, wired to one database, one fake GCS and one fake
    Pub/Sub broker — the same three pieces of "infrastructure" the API
    itself is wired to in the `client` fixture.
    """
    factory = _SharedSessionFactory(db_session)
    publisher = EventPublisher(fake_pubsub_client, enabled=True)
    storage = StorageService(fake_gcs_client)

    class _Chain:
        outbox = OutboxPublisherWorker(publisher=publisher, session_factory=factory)
        files = FileProcessingWorker(
            storage_service=storage,
            publisher=publisher,
            session_factory=factory,
            subscription="file-sub",
        )
        thumbnails = ThumbnailWorker(
            storage_service=storage, session_factory=factory, subscription="thumb-sub"
        )
        notifications = NotificationWorker(session_factory=factory, subscription="notify-sub")

    return _Chain()


def _message_for(envelope: EventEnvelope, *, delivery_attempt: int = 1) -> FakeMessage:
    data, attributes = envelope.to_pubsub_message()
    return FakeMessage(data, attributes, delivery_attempt=delivery_attempt)


# ---------------------------------------------------------------------
# The full chain
# ---------------------------------------------------------------------
async def test_an_image_upload_travels_the_whole_chain_to_a_thumbnail_and_a_notification(
    authed_client, db_session, fake_gcs_client, fake_pubsub_client, chain
):
    """
    The one test that proves Phase 8 actually works as a system rather
    than as five components that each pass their own tests.
    """
    # --- stage 1: the API writes an outbox row, publishes nothing -----
    file = await _upload(
        authed_client, filename="holiday.png", content=_png_bytes(), content_type="image/png"
    )

    [row] = await _outbox_rows(db_session)
    assert row.event_type == EventType.FILE_UPLOADED.value
    assert row.status == OutboxEventStatus.PENDING
    assert str(row.aggregate_id) == file["id"]
    # The request path must never talk to Pub/Sub — that is the entire
    # point of the outbox. If this fires, someone published inline.
    assert fake_pubsub_client.total_published == 0

    # --- stage 2: the outbox publisher moves it onto the wire ---------
    result = await chain.outbox.poll_once()
    assert (result.fetched, result.published, result.failed) == (1, 1, 0)

    await db_session.refresh(row)
    assert row.status == OutboxEventStatus.PUBLISHED
    assert row.published_at is not None

    [uploaded] = _published(fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.FILE_UPLOADED)
    # The event id survives the round trip: it is the idempotency key, so
    # a regenerated one here would silently break every consumer's
    # deduplication downstream.
    assert uploaded.event_id == row.event_id
    assert uploaded.payload["object_name"] == file["object_name"]
    assert uploaded.payload["content_type"] == "image/png"

    # --- stage 3: the file worker validates and fans out --------------
    upload_message = _message_for(uploaded)
    await chain.files._handle(upload_message)
    assert upload_message.acked is True

    processed = await _processed_rows(db_session)
    assert [p.consumer_name for p in processed] == ["file-processing-worker"]
    assert processed[0].status == ProcessedEventStatus.SUCCEEDED
    assert processed[0].event_id == uploaded.event_id

    [thumbnail_request] = _published(
        fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.THUMBNAIL_REQUESTED
    )
    [notification_request] = _published(
        fake_pubsub_client, SETTINGS.NOTIFICATION_EVENTS_TOPIC, EventType.NOTIFICATION_REQUESTED
    )
    # Both children belong to the user's original operation, and both
    # name the event that caused them.
    for child in (thumbnail_request, notification_request):
        assert child.correlation_id == uploaded.correlation_id
        assert child.causation_id == uploaded.event_id
        assert child.producer == "file-processing-worker"

    # --- stage 4: the thumbnail worker renders and records ------------
    thumbnail_message = _message_for(thumbnail_request)
    await chain.thumbnails._handle(thumbnail_message)
    assert thumbnail_message.acked is True

    stored = await db_session.get(FileMetadata, uuid.UUID(file["id"]))
    await db_session.refresh(stored)
    assert stored.thumbnail_object_name == f"thumbnails/{file['id']}.png"

    thumbnail_blob = fake_gcs_client.bucket(SETTINGS.GCS_BUCKET_NAME).blob(stored.thumbnail_object_name)
    assert thumbnail_blob.exists()
    with Image.open(io.BytesIO(thumbnail_blob.download_as_bytes())) as rendered:
        assert rendered.format == "PNG"
        assert max(rendered.size) <= SETTINGS.THUMBNAIL_MAX_DIMENSION_PX

    # --- stage 5: the notification worker records the notification ----
    notification_message = _message_for(notification_request)
    await chain.notifications._handle(notification_message)
    assert notification_message.acked is True

    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].notification_type == "file_ready"
    assert "holiday.png" in notifications[0].subject
    assert str(notifications[0].related_file_id) == file["id"]

    # --- and the ledger, across all three consumers -------------------
    ledger = {p.consumer_name: p.status for p in await _processed_rows(db_session)}
    assert ledger == {
        "file-processing-worker": ProcessedEventStatus.SUCCEEDED,
        "thumbnail-worker": ProcessedEventStatus.SUCCEEDED,
        "notification-worker": ProcessedEventStatus.SUCCEEDED,
    }


async def test_a_non_image_upload_reaches_the_notification_but_never_the_thumbnail_worker(
    authed_client, db_session, fake_pubsub_client, chain
):
    """
    Most uploads are not images. The chain must treat that as the normal
    case — no thumbnail request published, no failure recorded anywhere.
    """
    await _upload(
        authed_client, filename="notes.txt", content=b"plain text, not an image", content_type="text/plain"
    )
    await chain.outbox.poll_once()

    [uploaded] = _published(fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.FILE_UPLOADED)
    await chain.files._handle(_message_for(uploaded))

    assert _published(fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.THUMBNAIL_REQUESTED) == []
    [notification_request] = _published(
        fake_pubsub_client, SETTINGS.NOTIFICATION_EVENTS_TOPIC, EventType.NOTIFICATION_REQUESTED
    )

    await chain.notifications._handle(_message_for(notification_request))
    notifications = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert all(p.status == ProcessedEventStatus.SUCCEEDED for p in await _processed_rows(db_session))


async def test_redelivering_every_message_in_the_chain_changes_nothing(
    authed_client, db_session, fake_gcs_client, fake_pubsub_client, chain
):
    """
    At-least-once delivery is the assumption the entire design is built
    on, so the chain has to be run twice and produce the same end state.

    Note what makes this pass: the derived event ids are UUIDv5 over the
    parent event id, so the second fan-out publishes the *same*
    thumbnail/notification event ids. A `uuid4()` there would produce
    fresh identities on every retry and defeat deduplication silently —
    the failure this test exists to catch.
    """
    file = await _upload(
        authed_client, filename="repeat.png", content=_png_bytes(), content_type="image/png"
    )
    await chain.outbox.poll_once()
    [uploaded] = _published(fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.FILE_UPLOADED)

    for attempt in (1, 2):
        await chain.files._handle(_message_for(uploaded, delivery_attempt=attempt))
        # Take the LATEST of each derived type: the second fan-out really
        # does publish again (the file worker's own dedup absorbs the
        # parent, so on attempt 2 nothing new is published and this is the
        # same envelope as attempt 1 — which is precisely the point).
        thumbnail_request = _published(
            fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.THUMBNAIL_REQUESTED
        )[-1]
        notification_request = _published(
            fake_pubsub_client, SETTINGS.NOTIFICATION_EVENTS_TOPIC, EventType.NOTIFICATION_REQUESTED
        )[-1]
        await chain.thumbnails._handle(_message_for(thumbnail_request, delivery_attempt=attempt))
        await chain.notifications._handle(
            _message_for(notification_request, delivery_attempt=attempt)
        )

    # Exactly one ledger row per consumer, despite two deliveries each.
    assert len(await _processed_rows(db_session)) == 3
    # One notification, not two — the user is not told twice.
    assert len((await db_session.execute(select(Notification))).scalars().all()) == 1
    # One thumbnail object, not two — the name is deterministic.
    thumbnails = [
        name
        for name in fake_gcs_client.bucket(SETTINGS.GCS_BUCKET_NAME).store
        if name.startswith("thumbnails/")
    ]
    assert thumbnails == [f"thumbnails/{file['id']}.png"]


async def test_a_publish_outage_leaves_the_event_durable_and_replayable(
    authed_client, db_session, fake_pubsub_client, chain
):
    """
    The failure mode the outbox exists for: Pub/Sub is unavailable at the
    moment of publishing. The user's upload must still have succeeded,
    and the event must still be sitting in Postgres waiting — not lost.
    """
    await _upload(
        authed_client, filename="durable.png", content=_png_bytes(), content_type="image/png"
    )
    fake_pubsub_client.start_failing()

    result = await chain.outbox.poll_once()
    assert (result.fetched, result.published, result.failed) == (1, 0, 1)

    [row] = await _outbox_rows(db_session)
    assert row.status == OutboxEventStatus.FAILED  # "retry after next_attempt_at", not "give up"
    assert row.attempt_count == 1
    assert row.published_at is None
    assert row.last_error

    # Pub/Sub comes back. The row is due again once its backoff elapses;
    # `is_due` is what the next poll consults, so this asserts the row is
    # genuinely replayable rather than merely still present.
    fake_pubsub_client.stop_failing()
    row.next_attempt_at = row.created_at
    await db_session.flush()

    second = await chain.outbox.poll_once()
    assert (second.fetched, second.published) == (1, 1)
    assert row.status == OutboxEventStatus.PUBLISHED
    assert len(_published(fake_pubsub_client, SETTINGS.FILE_EVENTS_TOPIC, EventType.FILE_UPLOADED)) == 1
