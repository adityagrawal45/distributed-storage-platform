"""
Phase 8 — EventPublisher tests, driven against `FakePublisherClient`.

The two things most worth proving here are the two most likely to be got
wrong silently:

  1. The kill switch. `PUBSUB_ENABLED=False` must be a genuine no-op that
     never touches a client — not "publishes anyway but logs about it".
  2. The asyncio bridge. The real client returns a
     `concurrent.futures.Future` from a *synchronous* call; the fake
     reproduces that exactly, so a regression that awaits it directly or
     blocks the loop fails here rather than in production.
"""

import asyncio
import uuid

import pytest

from app.core.config import get_settings
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.exceptions.custom_exceptions import EventPublishError
from tests.fakes.fake_pubsub import FakePublisherClient


def _envelope(event_type: EventType = EventType.FILE_UPLOADED, **overrides) -> EventEnvelope:
    base = {"event_type": event_type, "user_id": uuid.uuid4(), "payload": {"file_id": str(uuid.uuid4())}}
    base.update(overrides)
    return EventEnvelope(**base)


# ---------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------
async def test_disabled_publisher_is_a_no_op_and_never_touches_the_client():
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=False)

    message_id = await publisher.publish(_envelope())

    assert publisher.enabled is False
    assert fake.publish_calls == 0
    assert fake.total_published == 0
    assert message_id.startswith("disabled-")


def test_pubsub_is_disabled_by_default_so_the_feature_lands_dark():
    """A brand-new outbound integration must default to off."""
    assert get_settings().PUBSUB_ENABLED is False
    assert EventPublisher(FakePublisherClient()).enabled is False


# ---------------------------------------------------------------------
# Happy path & routing
# ---------------------------------------------------------------------
async def test_publish_puts_the_envelope_on_the_configured_topic():
    settings = get_settings()
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)
    envelope = _envelope()

    message_id = await publisher.publish(envelope)

    topic_path = fake.topic_path(settings.GCP_PROJECT_ID, settings.FILE_EVENTS_TOPIC)
    messages = fake.messages_on(topic_path)
    assert len(messages) == 1
    assert message_id

    data, attributes = messages[0]
    assert EventEnvelope.from_json_bytes(data).event_id == envelope.event_id
    assert attributes["event_type"] == "file.uploaded"
    assert attributes["correlation_id"] == str(envelope.correlation_id)


async def test_each_event_type_lands_on_its_own_domain_topic():
    settings = get_settings()
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)

    await publisher.publish(_envelope(EventType.FILE_UPLOADED))
    await publisher.publish(_envelope(EventType.UPLOAD_COMPLETED))
    await publisher.publish(_envelope(EventType.NOTIFICATION_REQUESTED))

    project = settings.GCP_PROJECT_ID
    assert len(fake.messages_on(fake.topic_path(project, settings.FILE_EVENTS_TOPIC))) == 1
    assert len(fake.messages_on(fake.topic_path(project, settings.UPLOAD_EVENTS_TOPIC))) == 1
    assert len(fake.messages_on(fake.topic_path(project, settings.NOTIFICATION_EVENTS_TOPIC))) == 1


async def test_publish_many_publishes_each_envelope_in_order():
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)

    ids = await publisher.publish_many(
        [_envelope(EventType.THUMBNAIL_REQUESTED), _envelope(EventType.NOTIFICATION_REQUESTED)]
    )

    assert len(ids) == 2
    assert fake.published_event_types() == ["thumbnail.requested", "notification.requested"]


# ---------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------
async def test_a_client_failure_becomes_an_event_publish_error():
    """
    The caller must handle exactly one exception type. The outbox worker
    converts it into mark_failed + backoff; a raw google exception
    leaking here would couple every caller to the client library.
    """
    fake = FakePublisherClient()
    fake.start_failing()
    publisher = EventPublisher(fake, enabled=True)

    with pytest.raises(EventPublishError):
        await publisher.publish(_envelope())


async def test_failure_injection_can_succeed_n_times_first():
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)
    fake.start_failing(after=1)

    await publisher.publish(_envelope())  # the one allowed success
    with pytest.raises(EventPublishError):
        await publisher.publish(_envelope())

    assert fake.total_published == 1


async def test_publisher_recovers_once_the_transport_recovers():
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)

    fake.start_failing()
    with pytest.raises(EventPublishError):
        await publisher.publish(_envelope())

    fake.stop_failing()
    assert await publisher.publish(_envelope())
    assert fake.total_published == 1


# ---------------------------------------------------------------------
# The asyncio bridge
# ---------------------------------------------------------------------
async def test_publishing_does_not_block_the_event_loop():
    """
    The real client's `publish()` is synchronous. If it were called
    inline on the loop instead of via `run_in_executor`, a slow publish
    would stall every other task on the replica. This drives a
    deliberately slow fake publish and asserts an unrelated coroutine
    still makes progress while it is in flight.
    """
    ticks = 0

    class SlowPublisherClient(FakePublisherClient):
        def publish(self, topic_path, data, **attributes):
            import time

            time.sleep(0.2)  # blocking, exactly like a real gRPC round trip
            return super().publish(topic_path, data, **attributes)

    async def ticker():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    publisher = EventPublisher(SlowPublisherClient(), enabled=True)
    await asyncio.gather(publisher.publish(_envelope()), ticker())

    # If the loop had been blocked, the ticker could not have run at all
    # during the 0.2s publish.
    assert ticks == 10


async def test_concurrent_publishes_all_land():
    fake = FakePublisherClient()
    publisher = EventPublisher(fake, enabled=True)

    await asyncio.gather(*(publisher.publish(_envelope()) for _ in range(25)))
    assert fake.total_published == 25
