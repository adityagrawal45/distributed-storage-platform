"""
EventPublisher — the one place NimbusFS hands a message to Pub/Sub.

Design decisions
----------------
- **The sync client is wrapped in `run_in_executor`.** `google-cloud-
  pubsub`'s `PublisherClient` is synchronous and returns a
  `concurrent.futures.Future`; calling it directly from a coroutine would
  block the event loop for the duration of a network round trip, stalling
  every other request that replica is serving. The call is therefore
  pushed to the default thread-pool executor and its
  `concurrent.futures.Future` is adapted with `asyncio.wrap_future`, so
  awaiting it yields control properly. This is the exact executor-
  wrapping approach Phase 3 already established for the equally-sync GCS
  client (`StorageService`), applied to the same problem.

- **`PUBSUB_ENABLED=False` is a real, documented no-op — not a stub.**
  When the switch is off the publisher logs at INFO and returns a
  synthetic message id without constructing a client at all. This is the
  same operational kill-switch pattern as `CACHE_ENABLED`, and it is the
  reason Phase 8 can land dark: with it off, the API writes outbox rows
  transactionally exactly as it will in production, they simply never
  leave Postgres. Flipping the switch — not deploying new code — turns
  the system on. It is also what lets the entire pre-existing test suite
  run untouched with no Pub/Sub anywhere.

- **The client is created lazily and cached per instance.** Building a
  `PublisherClient` opens gRPC channels; doing that at import time would
  make `import app.main` require credentials, breaking `alembic` and
  every offline tool. The `google.cloud.pubsub_v1` import itself is
  likewise deferred into the factory so the package is not a hard import
  dependency of the API process.

- **`publish()` raises `EventPublishError`, never a raw google
  exception.** Callers (the outbox worker, the file-processing worker)
  handle one exception type and stay decoupled from the client library.
  The outbox worker converts it into `mark_failed` + backoff, so a
  publish failure costs a retry, never a lost event.
"""

import asyncio
import os
from typing import Any

from app.core.config import get_settings
from app.core.metrics import PUBSUB_MESSAGES_PUBLISHED_TOTAL, safe_call
from app.events.envelope import EventEnvelope
from app.events.topics import topic_for_event_type
from app.exceptions.custom_exceptions import EventPublishError
from app.logging.logger import get_logger

logger = get_logger(__name__)


def build_publisher_client() -> Any:
    """
    Constructs a real `google.cloud.pubsub_v1.PublisherClient`.

    The import lives inside the function on purpose (see module
    docstring). `PUBSUB_EMULATOR_HOST` is exported into the process
    environment because that is the only channel the google client
    library reads it from — there is no constructor argument for it.
    """
    settings = get_settings()
    if settings.PUBSUB_EMULATOR_HOST:
        os.environ["PUBSUB_EMULATOR_HOST"] = settings.PUBSUB_EMULATOR_HOST

    from google.cloud import pubsub_v1  # noqa: PLC0415 - deferred by design

    return pubsub_v1.PublisherClient()


class EventPublisher:
    """
    Publishes `EventEnvelope`s to their configured topic.

    `client` is injectable so tests can pass `FakePublisherClient`
    (`tests/fakes/fake_pubsub.py`) — the same "inject the SDK client, not
    the service wrapper" technique `get_gcs_client` uses for GCS.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        project_id: str | None = None,
        enabled: bool | None = None,
    ):
        settings = get_settings()
        self._client = client
        self._project_id = project_id or settings.GCP_PROJECT_ID
        self._enabled = settings.PUBSUB_ENABLED if enabled is None else enabled
        # Cache of resolved topic paths; `topic_path()` is pure string
        # formatting but resolving it once per topic keeps the hot loop
        # allocation-free.
        self._topic_paths: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = build_publisher_client()
        return self._client

    def _topic_path(self, topic_name: str) -> str:
        if topic_name not in self._topic_paths:
            self._topic_paths[topic_name] = self._resolve_client().topic_path(self._project_id, topic_name)
        return self._topic_paths[topic_name]

    async def publish(self, envelope: EventEnvelope) -> str:
        """
        Publishes one envelope and returns the Pub/Sub message id.

        Raises `EventPublishError` on any failure — the caller decides
        whether that means "retry this outbox row later" or "NACK this
        message".
        """
        topic_name = topic_for_event_type(envelope.event_type)

        if not self._enabled:
            # Documented production no-op. Logged at INFO (not DEBUG) so
            # "why did nothing arrive downstream?" is answerable from the
            # logs of a replica that has the switch off.
            logger.info(
                "event_publish_skipped_pubsub_disabled",
                event_id=str(envelope.event_id),
                event_type=envelope.event_type.value,
                topic=topic_name,
            )
            safe_call(
                lambda: PUBSUB_MESSAGES_PUBLISHED_TOTAL.labels(topic=topic_name, result="disabled").inc(),
                operation="pubsub_published_inc",
            )
            return f"disabled-{envelope.event_id}"

        data, attributes = envelope.to_pubsub_message()
        topic_path = self._topic_path(topic_name)

        try:
            loop = asyncio.get_running_loop()
            # Two hops, both necessary:
            #  1. run_in_executor  — the client's `publish()` call itself
            #     is sync and can block on gRPC channel setup.
            #  2. wrap_future      — its return value is a
            #     `concurrent.futures.Future`, not awaitable on its own.
            concurrent_future = await loop.run_in_executor(
                None, lambda: self._resolve_client().publish(topic_path, data, **attributes)
            )
            message_id = await asyncio.wrap_future(concurrent_future)
        except Exception as exc:  # noqa: BLE001 - normalized into one domain error by design
            logger.error(
                "event_publish_failed",
                event_id=str(envelope.event_id),
                event_type=envelope.event_type.value,
                topic=topic_name,
                error=str(exc),
            )
            safe_call(
                lambda: PUBSUB_MESSAGES_PUBLISHED_TOTAL.labels(topic=topic_name, result="failure").inc(),
                operation="pubsub_published_inc",
            )
            raise EventPublishError(f"Failed to publish {envelope.event_type.value}: {exc}") from exc

        logger.info(
            "event_published",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type.value,
            topic=topic_name,
            message_id=str(message_id),
            correlation_id=str(envelope.correlation_id),
        )
        safe_call(
            lambda: PUBSUB_MESSAGES_PUBLISHED_TOTAL.labels(topic=topic_name, result="success").inc(),
            operation="pubsub_published_inc",
        )
        return str(message_id)

    async def publish_many(self, envelopes: list[EventEnvelope]) -> list[str]:
        """
        Convenience batch helper.

        Deliberately sequential, not `asyncio.gather`: the only current
        caller is the file-processing worker publishing two follow-up
        events, and sequential publishing keeps the failure story simple
        (the first failure raises, and the caller retries the whole
        message — which is safe because consumers are idempotent). The
        outbox worker does NOT use this; it publishes per row so one bad
        row cannot take its siblings down with it.
        """
        return [await self.publish(envelope) for envelope in envelopes]
