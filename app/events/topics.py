"""
Event-type -> topic routing.

Design decision: **three domain topics, not one firehose and not one
topic per event type.**

- One topic for everything would force every worker to receive every
  event and discard most of them — paying delivery cost, ack cost and
  (worse) DLQ/backlog coupling for messages it has no interest in. A
  thumbnail worker falling behind would then also delay notifications.
- One topic per event type (12 topics) would make adding an event type a
  Terraform/IAM change and would scatter a consumer's subscription set
  across many resources for no isolation benefit, since the consumers
  that care about `file.moved` also care about `file.renamed`.

Three is the number of genuinely distinct *fan-out boundaries* today:

  FILE_EVENTS_TOPIC          all `file.*` and `folder.*` lifecycle events,
                             plus `thumbnail.requested` — consumed by the
                             file-processing worker and the thumbnail
                             worker, which filter by `event_type`.
  UPLOAD_EVENTS_TOPIC        `upload.completed` — the chunked-upload
                             lifecycle, kept separate because its
                             consumers are about *upload sessions*, a
                             different aggregate with a different
                             retention/replay story than file metadata.
  NOTIFICATION_EVENTS_TOPIC  `notification.requested` — a pure egress
                             boundary. Isolated so a wedged notification
                             backlog (a down email provider, in a future
                             phase) can never apply backpressure to file
                             processing.

**No ordering keys in Phase 8.** Every consumer shipped this phase is an
idempotent projection with no cross-event sequencing requirement: a
thumbnail is regenerated from the current object, a notification row is
append-only, and file-processing validation reads current state. Ordering
keys would serialize delivery per key and cap throughput at one in-flight
message per aggregate — a real cost paid for a guarantee nothing needs
yet. `aggregate_id` is nonetheless captured on every `OutboxEvent`, so
turning on ordering later is a publisher-side change with no migration.
"""

from app.core.config import get_settings
from app.events.envelope import EventType


def _topic_names() -> dict[str, str]:
    settings = get_settings()
    return {
        "file": settings.FILE_EVENTS_TOPIC,
        "upload": settings.UPLOAD_EVENTS_TOPIC,
        "notification": settings.NOTIFICATION_EVENTS_TOPIC,
    }


# Event type -> logical topic group. Resolved to a concrete, configured
# topic NAME at call time (not import time) so tests and per-environment
# settings overrides take effect without reloading this module.
EVENT_TYPE_TO_TOPIC: dict[EventType, str] = {
    EventType.FILE_UPLOADED: "file",
    EventType.FILE_COMPLETED: "file",
    EventType.FILE_DELETED: "file",
    EventType.FILE_RESTORED: "file",
    EventType.FILE_MOVED: "file",
    EventType.FILE_RENAMED: "file",
    EventType.FILE_VERSION_CREATED: "file",
    EventType.FOLDER_CREATED: "file",
    EventType.FOLDER_DELETED: "file",
    EventType.THUMBNAIL_REQUESTED: "file",
    EventType.UPLOAD_COMPLETED: "upload",
    EventType.NOTIFICATION_REQUESTED: "notification",
}


def topic_for_event_type(event_type: EventType | str) -> str:
    """
    Resolves an event type to its configured topic *name*.

    Accepts a raw string too, because the outbox stores `event_type` as a
    plain column and the publisher worker reads it back as a string — an
    unrecognized value raises `KeyError` rather than silently defaulting
    to some topic, so a typo surfaces as a loud publish failure on that
    one row instead of quietly polluting an unrelated topic.
    """
    if isinstance(event_type, str):
        event_type = EventType(event_type)
    return _topic_names()[EVENT_TYPE_TO_TOPIC[event_type]]


def all_topic_names() -> list[str]:
    """Every configured topic name — used by local emulator bootstrap tooling."""
    return list(dict.fromkeys(_topic_names().values()))
