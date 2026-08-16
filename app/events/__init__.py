"""
Event-driven infrastructure (Phase 8).

Why this is a top-level package (`app/events/`) rather than living under
`app/core/`: in this codebase `app/core/` is reserved for configuration
and security/coordination *primitives* (settings, password hashing, JWT,
retry, circuit breaker, locks, cache key/serializer/policy). The event
envelope is not a primitive — it is a domain contract shared by two
different kinds of process (the FastAPI API, which produces events, and
the standalone workers in `app/workers/`, which consume them). It sits
beside `app/models/` and `app/services/` for the same reason those do.
"""

from app.events.envelope import EventEnvelope, EventType
from app.events.topics import EVENT_TYPE_TO_TOPIC, topic_for_event_type

__all__ = [
    "EventEnvelope",
    "EventType",
    "EVENT_TYPE_TO_TOPIC",
    "topic_for_event_type",
]
