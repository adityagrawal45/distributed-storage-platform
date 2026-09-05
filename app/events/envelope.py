"""
The event envelope — NimbusFS's wire contract for every domain event.

Design decisions
----------------
- **Envelope vs. payload.** Everything a *consumer framework* needs
  (identity, type, version, timing, correlation, tenancy, actor) lives in
  fixed, typed envelope fields; everything a *specific handler* needs
  lives in the free-form `payload` dict. This split is what lets
  `BaseWorker` parse, deduplicate, log and route a message it knows
  nothing about the semantics of.

- **`event_id` is the idempotency key.** It is generated once, at
  outbox-insert time, and never regenerated on republish. Pub/Sub is
  at-least-once, and the transactional outbox itself can legitimately
  republish a row whose `mark_published` commit failed after Pub/Sub had
  already accepted the message. Consumers deduplicate on `event_id` via
  `ProcessedEvent`'s `UniqueConstraint(event_id, consumer_name)`, so a
  stable `event_id` across redeliveries is load-bearing, not cosmetic.

- **`correlation_id` / `causation_id` are different things.**
  `correlation_id` ties everything back to the one client operation that
  started the chain (read from the same `structlog.contextvars` that
  `RequestContextMiddleware` binds per request, so a worker's log lines
  join a user's original HTTP request without any manual threading).
  `causation_id` is the *immediately preceding* event's `event_id` — it
  builds the parent/child chain when a worker publishes a follow-up event
  (e.g. `file.uploaded` causes `thumbnail.requested`). Correlation is the
  whole tree; causation is one edge in it.

- **`tenant_id` is reserved and always null today.** NimbusFS has no
  multi-tenancy yet (see the object-naming scheme's hardcoded "default"
  tenant segment in `StorageService.generate_object_name`). It is in the
  envelope from day one because adding a field to an event contract that
  is already in flight is a versioning event, while shipping a nullable
  field that is simply never populated costs nothing.

- **Versioning contract.** `event_version` starts at 1 per event type.
  *Additive* changes (new optional `payload` keys) do NOT bump it —
  consumers must ignore unknown payload keys, and the pydantic model here
  does not forbid extras in `payload` because `payload` is a plain dict.
  A version bump is reserved for a **breaking** change: removing a
  payload key, renaming one, or changing a value's type/meaning. When
  that happens, publish both versions in parallel until every consumer
  has been rolled forward, then retire the old one — a consumer that
  receives a version it does not understand must treat it as
  non-retryable rather than crash-looping on it.

- **Attributes carry the routing/filtering fields.** `to_pubsub_message()`
  returns `(data_bytes, attributes)` where attributes duplicate
  `event_type` / `event_version` / `correlation_id`. Pub/Sub subscription
  filters operate on attributes only, so exposing these lets a future
  subscription narrow its delivery server-side without every consumer
  having to deserialize and discard messages it never wanted. Attribute
  values must be strings — Pub/Sub enforces this — hence the explicit
  `str()` conversions.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """
    The complete NimbusFS domain event catalog as of Phase 8.

    Names are `{aggregate}.{past-tense-verb}` — events describe what
    *has already happened*, never a command to do something. The two
    `*.requested` values are the deliberate exception: they are
    worker-to-worker work requests, published by the file-processing
    worker rather than by the API, and named so the distinction is
    visible in a log line without needing to know the publisher.
    """

    FILE_UPLOADED = "file.uploaded"
    FILE_COMPLETED = "file.completed"
    FILE_DELETED = "file.deleted"
    FILE_RESTORED = "file.restored"
    FILE_MOVED = "file.moved"
    FILE_RENAMED = "file.renamed"
    FILE_VERSION_CREATED = "file.version.created"
    FOLDER_CREATED = "folder.created"
    FOLDER_DELETED = "folder.deleted"
    UPLOAD_COMPLETED = "upload.completed"
    THUMBNAIL_REQUESTED = "thumbnail.requested"
    NOTIFICATION_REQUESTED = "notification.requested"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventEnvelope(BaseModel):
    """
    The serialized form of every message on every NimbusFS topic.

    Pydantic v2 model (project-wide convention). `model_dump(mode="json")`
    produces the exact JSON that goes on the wire; `model_validate_json`
    parses it back on the consumer side, and a parse failure there is
    classified NON-retryable (a malformed message will never become
    well-formed on redelivery — see `BaseWorker._handle`).
    """

    model_config = ConfigDict(use_enum_values=False)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: EventType
    event_version: int = 1
    occurred_at: datetime = Field(default_factory=_utcnow)
    # Which component produced this — "api", "file-processing-worker", etc.
    # Purely observability, but the single most useful field when an
    # unexpected event shows up on a topic.
    producer: str = "api"

    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    causation_id: uuid.UUID | None = None
    # Reserved; see module docstring. Always None in Phase 8.
    tenant_id: uuid.UUID | None = None
    user_id: uuid.UUID
    # Phase 11: the distributed-tracing ID bound by `RequestContextMiddleware`
    # for the HTTP request that (directly or transitively) caused this
    # event, read from structlog's contextvars at emit time (see
    # `app/events/emitter.py::_emit_event`). Plain `str`, not `uuid.UUID` —
    # a caller-supplied `X-Trace-ID` header is honored verbatim and is not
    # guaranteed to be a UUID. Threading this through the outbox -> Pub/Sub
    # -> worker hop (`BaseWorker._handle` re-binds it into the worker's own
    # contextvars) is what lets an engineer grep logs across the WHOLE
    # chain — API request, worker execution, final processing — by one
    # `trace_id`, without needing a full OpenTelemetry collector. See
    # `app/core/tracing.py`'s module docstring for the full rationale.
    trace_id: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json_bytes(self) -> bytes:
        """UTF-8 JSON, exactly as it is put on the wire."""
        return self.model_dump_json().encode("utf-8")

    def to_pubsub_message(self) -> tuple[bytes, dict[str, str]]:
        """
        Returns `(data, attributes)` for `PublisherClient.publish`.

        Attributes intentionally duplicate three envelope fields so
        subscription filters can select on them without the server having
        to look inside the payload. Pub/Sub attribute values must be
        strings.
        """
        attributes = {
            "event_type": self.event_type.value,
            "event_version": str(self.event_version),
            "correlation_id": str(self.correlation_id),
        }
        if self.trace_id:
            attributes["trace_id"] = self.trace_id
        return self.to_json_bytes(), attributes

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "EventEnvelope":
        """Inverse of `to_json_bytes`. Raises `pydantic.ValidationError` on bad input."""
        return cls.model_validate_json(data)
