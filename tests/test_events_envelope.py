"""
Phase 8 — event envelope & topic-routing tests.

Pure unit tests: no I/O, no fixtures, no database. The envelope is the
wire contract every other Phase 8 component depends on, so it is tested
first and in isolation (mirroring how Phase 7 tested `CacheKeyBuilder`/
`CacheSerializer` before anything that used them).
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.events.envelope import EventEnvelope, EventType
from app.events.topics import EVENT_TYPE_TO_TOPIC, all_topic_names, topic_for_event_type


def _envelope(**overrides) -> EventEnvelope:
    base = {
        "event_type": EventType.FILE_UPLOADED,
        "user_id": uuid.uuid4(),
        "payload": {"file_id": str(uuid.uuid4())},
    }
    base.update(overrides)
    return EventEnvelope(**base)


# ---------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------
def test_event_catalog_has_the_twelve_phase_8_types():
    assert len(EventType) == 12
    assert EventType.FILE_UPLOADED.value == "file.uploaded"
    assert EventType.FILE_VERSION_CREATED.value == "file.version.created"
    assert EventType.NOTIFICATION_REQUESTED.value == "notification.requested"


def test_every_event_type_routes_to_a_topic():
    """A new event type with no topic mapping must fail loudly here, not at publish time in production."""
    for event_type in EventType:
        assert event_type in EVENT_TYPE_TO_TOPIC
        assert topic_for_event_type(event_type)


def test_topic_grouping_matches_the_three_fan_out_boundaries():
    settings = get_settings()
    assert topic_for_event_type(EventType.FILE_UPLOADED) == settings.FILE_EVENTS_TOPIC
    assert topic_for_event_type(EventType.FOLDER_DELETED) == settings.FILE_EVENTS_TOPIC
    assert topic_for_event_type(EventType.THUMBNAIL_REQUESTED) == settings.FILE_EVENTS_TOPIC
    assert topic_for_event_type(EventType.UPLOAD_COMPLETED) == settings.UPLOAD_EVENTS_TOPIC
    assert topic_for_event_type(EventType.NOTIFICATION_REQUESTED) == settings.NOTIFICATION_EVENTS_TOPIC
    assert len(all_topic_names()) == 3


def test_topic_lookup_accepts_a_raw_string_event_type():
    """The outbox stores event_type as a plain column; the publisher reads it back as str."""
    assert topic_for_event_type("file.uploaded") == get_settings().FILE_EVENTS_TOPIC


def test_unknown_event_type_string_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        topic_for_event_type("file.teleported")


# ---------------------------------------------------------------------
# Envelope defaults & required fields
# ---------------------------------------------------------------------
def test_envelope_generates_its_own_ids_and_timestamp():
    envelope = _envelope()
    assert isinstance(envelope.event_id, uuid.UUID)
    assert isinstance(envelope.correlation_id, uuid.UUID)
    assert envelope.occurred_at.tzinfo is not None
    assert envelope.occurred_at <= datetime.now(timezone.utc)


def test_envelope_defaults_are_the_documented_ones():
    envelope = _envelope()
    assert envelope.event_version == 1
    assert envelope.producer == "api"
    assert envelope.causation_id is None
    # Reserved for a future multi-tenancy phase; must be null today.
    assert envelope.tenant_id is None


def test_user_id_is_required():
    with pytest.raises(ValidationError):
        EventEnvelope(event_type=EventType.FILE_UPLOADED)


def test_event_type_must_be_in_the_catalog():
    with pytest.raises(ValidationError):
        EventEnvelope(event_type="file.teleported", user_id=uuid.uuid4())


# ---------------------------------------------------------------------
# Serialization round trip
# ---------------------------------------------------------------------
def test_json_round_trip_preserves_every_field():
    original = _envelope(
        causation_id=uuid.uuid4(),
        producer="file-processing-worker",
        payload={"file_id": "abc", "size": 10, "nested": {"a": [1, 2]}},
    )
    restored = EventEnvelope.from_json_bytes(original.to_json_bytes())

    assert restored.event_id == original.event_id
    assert restored.event_type == original.event_type
    assert restored.event_version == original.event_version
    assert restored.correlation_id == original.correlation_id
    assert restored.causation_id == original.causation_id
    assert restored.user_id == original.user_id
    assert restored.producer == original.producer
    assert restored.payload == original.payload
    assert restored.occurred_at == original.occurred_at


def test_wire_format_is_plain_utf8_json_not_pickle():
    """Same reasoning as Phase 7's cache serializer: a message must be readable during an incident."""
    envelope = _envelope()
    decoded = json.loads(envelope.to_json_bytes().decode("utf-8"))
    assert decoded["event_type"] == "file.uploaded"
    assert decoded["event_version"] == 1


def test_pubsub_attributes_expose_the_filterable_fields_as_strings():
    envelope = _envelope()
    data, attributes = envelope.to_pubsub_message()

    assert data == envelope.to_json_bytes()
    assert attributes == {
        "event_type": "file.uploaded",
        "event_version": "1",
        "correlation_id": str(envelope.correlation_id),
    }
    # Pub/Sub rejects non-string attribute values.
    assert all(isinstance(v, str) for v in attributes.values())


def test_malformed_bytes_raise_a_validation_error_the_worker_can_classify():
    with pytest.raises(ValidationError):
        EventEnvelope.from_json_bytes(b"{not json at all")


def test_unknown_payload_keys_are_preserved_not_rejected():
    """
    The documented additive-change contract: a producer that adds a new
    payload key must NOT break an older consumer.
    """
    envelope = _envelope(payload={"file_id": "x", "a_field_added_in_a_later_build": True})
    restored = EventEnvelope.from_json_bytes(envelope.to_json_bytes())
    assert restored.payload["a_field_added_in_a_later_build"] is True


def test_event_version_can_be_bumped_for_a_breaking_change():
    envelope = _envelope(event_version=2)
    _, attributes = envelope.to_pubsub_message()
    assert attributes["event_version"] == "2"
    assert EventEnvelope.from_json_bytes(envelope.to_json_bytes()).event_version == 2
