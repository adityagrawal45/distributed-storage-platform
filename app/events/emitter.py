"""
`OutboxEmitterMixin` — how a service emits a domain event.

Why a mixin rather than four copies of the same private method: the
emit logic (build an envelope, read correlation context, insert an outbox
row, never raise) is identical across `FileUploadService`,
`ChunkedUploadService`, `MetadataService` and `FolderService`. Four
copies would be four places to fix when the envelope gains a field. The
mixin adds exactly one method and one attribute contract, and does not
participate in `__init__` — each service still assigns `self._outbox`
itself, keeping construction explicit and matching how `self._cache` /
`self._invalidator` were added in Phase 7.

Three design decisions worth spelling out
-----------------------------------------

**1. `outbox=None` means no-op, and that is the backward-compatibility
mechanism.** Every service takes `outbox: OutboxRepository | None = None`
as a *keyword-only* parameter defaulting to `None`, exactly as Phase 7
added `cache=`/`invalidator=`. Every pre-existing construction — in
routes, in other services, in all 248 pre-Phase-8 tests — keeps working
untouched, and emits nothing. Only the DI providers pass a real
repository. This is why Phase 8 required zero edits to any existing test.

**2. Correlation and causation come from `structlog.contextvars`, not
from a parameter threaded through every method signature.**
`RequestContextMiddleware` already binds `correlation_id` per request for
logging, and `contextvars` survive `await` and `asyncio.to_thread`. Reading
it here means `move_file(...)` does not grow a `correlation_id` argument
that every caller and every test would have to pass. If nothing is bound
(a service constructed outside a request, e.g. in a worker), a fresh
correlation ID is generated — a new chain, which is the truthful thing to
record.

**3. Emitting never raises.** A failure to *build* an event must not fail
the user's upload. Note carefully what this does and does not cover: the
outbox INSERT itself is part of the request transaction, so if the
transaction rolls back the event row disappears with it — that is the
whole point, and is not something to swallow. What is swallowed is
programming-level failure in envelope construction (an unserializable
payload value, say). Those are logged at ERROR.
"""

import uuid
from typing import Any

import structlog

from app.events.envelope import EventEnvelope, EventType
from app.logging.logger import get_logger
from app.repositories.outbox_repository import OutboxRepository

logger = get_logger(__name__)


def _context_uuid(key: str) -> uuid.UUID | None:
    """
    Reads a UUID out of the current structlog context, tolerating a
    non-UUID value (a caller may have supplied an arbitrary
    `X-Correlation-ID` header string — `RequestContextMiddleware` honors
    it verbatim for logging, and we must not crash an upload over it).
    """
    raw = structlog.contextvars.get_contextvars().get(key)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


class OutboxEmitterMixin:
    """Mixed into every service that publishes domain events."""

    # Set by each service's `__init__`. Declared here so the contract is
    # visible and type checkers do not complain about the attribute.
    _outbox: OutboxRepository | None = None

    # Identifies the producing component in every envelope. Overridden by
    # workers, which are not "api".
    _event_producer: str = "api"

    @property
    def _events_enabled(self) -> bool:
        return self._outbox is not None

    async def _emit_event(
        self,
        event_type: EventType,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: dict[str, Any] | None = None,
        event_version: int = 1,
    ) -> EventEnvelope | None:
        """
        Inserts one PENDING outbox row into the CALLER'S open transaction.

        Returns the envelope (useful in tests and for chaining a
        `causation_id`), or `None` when no outbox is wired.
        """
        if self._outbox is None:
            return None

        try:
            envelope = EventEnvelope(
                event_type=event_type,
                event_version=event_version,
                producer=self._event_producer,
                correlation_id=_context_uuid("correlation_id") or uuid.uuid4(),
                causation_id=_context_uuid("causation_id"),
                user_id=user_id,
                payload=payload or {},
            )
            await self._outbox.add_event(
                event_id=envelope.event_id,
                event_type=envelope.event_type.value,
                event_version=envelope.event_version,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                correlation_id=envelope.correlation_id,
                causation_id=envelope.causation_id,
                user_id=envelope.user_id,
                # `mode="json"` so UUIDs/datetimes in the payload are
                # already JSON-native — the column is JSONB, and a raw
                # `uuid.UUID` would fail to serialize at flush time.
                payload=envelope.model_dump(mode="json")["payload"],
            )
        except Exception as exc:  # noqa: BLE001 - see decision #3 in the module docstring
            logger.error(
                "outbox_emit_failed",
                event_type=getattr(event_type, "value", str(event_type)),
                aggregate_id=str(aggregate_id),
                error=str(exc),
            )
            return None

        logger.info(
            "outbox_event_queued",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type.value,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
        )
        return envelope
