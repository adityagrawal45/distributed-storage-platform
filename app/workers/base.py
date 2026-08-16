"""
`BaseWorker` — the Pub/Sub consumer skeleton every event worker inherits.

The single most important structural decision here: **no business logic
lives in the raw Pub/Sub callback.** `_sync_callback` does exactly one
thing — hand the message across the thread boundary into the asyncio
loop — and `_handle` does exactly one thing beyond bookkeeping: `await
self.process(envelope, session)`. Subclasses implement `process()` and
nothing else.

That matters for two reasons. First, `process()` is an ordinary async
method taking an envelope and a session, so it is directly testable with
no Pub/Sub, no threads and no client library — which is how every worker
test in this phase is written. Second, the ack/nack decision is made in
exactly ONE place; if each worker made it for itself, they would drift,
and an inconsistent ack policy is how events get silently dropped.

The threading bridge
--------------------
`google-cloud-pubsub`'s streaming pull invokes its callback on a
**library-owned background thread**, not the event loop. Everything
NimbusFS does downstream (SQLAlchemy async, the GCS wrapper, the
publisher) is asyncio. `asyncio.run_coroutine_threadsafe(coro, loop)` is
the correct bridge: it schedules the coroutine on the main loop from
another thread and returns a `concurrent.futures.Future` the callback
thread blocks on. Blocking *that* thread is fine and in fact desirable —
it is how `FlowControl(max_messages=...)` exerts backpressure, capping
in-flight work at `WORKER_CONCURRENCY` instead of pulling unboundedly.

The ack/nack decision table
---------------------------
| Outcome                              | ProcessedEvent | Settle |
|--------------------------------------|----------------|--------|
| already processed (pre-check hit)     | (exists)       | ACK    |
| `process()` returns                   | SUCCEEDED      | ACK    |
| `NonRetryableEventError`              | FAILED         | ACK    |
| envelope fails to parse               | none*          | ACK    |
| `RetryableEventError` / anything else | none           | NACK   |
| lost idempotency race (IntegrityError)| (winner's)     | ACK    |

*A message whose bytes are not a valid envelope has no `event_id` to key
a `ProcessedEvent` on, so nothing can be recorded — it is logged at ERROR
and ACKed, because redelivering unparseable bytes produces the same
unparseable bytes forever.

Note what is NOT in that table: non-retryable failures are never routed
to the dead-letter topic. The DLQ is for *retry-exhausted* messages a
human might replay after a fix. See `NonRetryableEventError`'s docstring.
"""

import asyncio
import uuid
from abc import ABC, abstractmethod
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.server_info import get_server_identity
from app.database.session import AsyncSessionLocal
from app.events.envelope import EventEnvelope
from app.exceptions.custom_exceptions import NonRetryableEventError, RetryableEventError
from app.logging.logger import get_logger
from app.models.processed_event import ProcessedEventStatus
from app.repositories.processed_event_repository import ProcessedEventRepository
from app.workers.runtime import WorkerRuntimeMixin

logger = get_logger(__name__)


class BaseWorker(WorkerRuntimeMixin, ABC):
    """
    Subclasses set `worker_name` / `consumer_name` and implement
    `process()`. Everything else — subscription wiring, shutdown,
    heartbeat, idempotency, ack policy, structured logging context — is
    handled here.
    """

    #: Human-facing identity, used in logs and in the heartbeat.
    worker_name: str = "base-worker"
    #: Identity written into `ProcessedEvent.consumer_name`. Kept SEPARATE
    #: from `worker_name` on purpose: renaming a deployment must never
    #: silently reset a consumer's idempotency ledger, which would cause
    #: every historical event to look unprocessed on the next replay.
    consumer_name: str = "base-worker"

    def __init__(
        self,
        *,
        subscription: str | None = None,
        session_factory=None,
        subscriber_client: Any | None = None,
    ):
        self._settings = get_settings()
        self._subscription = subscription or self.default_subscription()
        self._session_factory = session_factory or AsyncSessionLocal
        self._subscriber = subscriber_client
        self._streaming_future = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight = 0
        self._init_runtime()

    def default_subscription(self) -> str:
        """Overridden by each worker to pull its own configured subscription name."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # The one method subclasses implement
    # ------------------------------------------------------------------
    @abstractmethod
    async def process(self, envelope: EventEnvelope, session: AsyncSession) -> None:
        """
        Do the work for one event.

        Contract:
        - Return normally => SUCCEEDED + ACK.
        - Raise `NonRetryableEventError` => FAILED + ACK (permanent).
        - Raise anything else => NACK (Pub/Sub redelivers with backoff).
        - MUST be idempotent. `ProcessedEvent` prevents *repeated* work in
          the common case, but a crash between doing the work and
          recording it will genuinely re-run `process()`.
        - MUST NOT commit. `_handle` owns the transaction boundary, so
          the work and its `ProcessedEvent` row commit together — the
          same "one commit point" discipline `get_db` enforces for the API.
        """

    def interested_in(self, envelope: EventEnvelope) -> bool:
        """
        Client-side event filter. Default: everything.

        Workers sharing a topic (the file worker and the thumbnail worker
        both consume FILE_EVENTS_TOPIC) override this to ignore event
        types they do not handle. An uninteresting message is ACKed
        without a `ProcessedEvent` row: it was not processed, it was
        declined, and recording it would pollute the ledger that answers
        "did this consumer handle this event?".
        """
        return True

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------
    async def _handle(self, message: Any) -> None:
        """
        The full per-message lifecycle. See the decision table in the
        module docstring. This method never raises: every path ends in
        exactly one `ack()` or one `nack()`, because a message that is
        neither acked nor nacked just stalls until the ack deadline
        expires — the worst of both outcomes.
        """
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            worker=self.worker_name,
            consumer=self.consumer_name,
            server_id=get_server_identity().instance_id,
            message_id=getattr(message, "message_id", None),
            delivery_attempt=getattr(message, "delivery_attempt", None),
        )
        self._in_flight += 1

        try:
            # --- parse: failure is permanent -------------------------
            try:
                envelope = EventEnvelope.from_json_bytes(message.data)
            except (ValidationError, ValueError, UnicodeDecodeError) as exc:
                logger.error("event_unparseable_discarded", error=str(exc))
                message.ack()  # redelivering the same bytes cannot help
                return

            structlog.contextvars.bind_contextvars(
                event_id=str(envelope.event_id),
                event_type=envelope.event_type.value,
                correlation_id=str(envelope.correlation_id),
            )

            if not self.interested_in(envelope):
                logger.debug("event_not_for_this_consumer")
                message.ack()
                return

            await self._process_with_session(envelope, message)
        finally:
            self._in_flight -= 1

    async def _process_with_session(self, envelope: EventEnvelope, message: Any) -> None:
        async with self._session_factory() as session:
            processed = ProcessedEventRepository(session)

            # --- pre-check: an OPTIMIZATION, not the guarantee --------
            try:
                if await processed.has_processed(envelope.event_id, self.consumer_name):
                    logger.info("event_already_processed_skipped")
                    message.ack()
                    return
            except Exception as exc:  # noqa: BLE001
                # The pre-check is only an optimization; if the database
                # is unreachable the work below will fail anyway and NACK.
                logger.warning("idempotency_precheck_failed", error=str(exc))

            status = ProcessedEventStatus.SUCCEEDED
            error_text: str | None = None

            try:
                await self.process(envelope, session)
            except NonRetryableEventError as exc:
                # Permanent. Record WHY and ACK — see the module
                # docstring on why this does not go to the DLQ.
                status = ProcessedEventStatus.FAILED
                error_text = exc.detail
                logger.warning("event_permanently_failed", error=exc.detail)
            except Exception as exc:  # noqa: BLE001
                # Retryable — including anything unexpected. "Try again"
                # is the safe default when we do not know what broke.
                retryable = isinstance(exc, RetryableEventError)
                logger.error(
                    "event_processing_failed_will_retry",
                    error=str(exc),
                    classified_retryable=retryable,
                    max_delivery_attempts=self._settings.MAX_DELIVERY_ATTEMPTS,
                )
                await session.rollback()
                message.nack()
                return

            # --- record the terminal outcome, then settle ------------
            try:
                _entry, created = await processed.record(
                    event_id=envelope.event_id,
                    consumer_name=self.consumer_name,
                    status=status,
                    error=error_text,
                )
                await session.commit()
                if not created:
                    logger.info("duplicate_event_absorbed")
            except IntegrityError:
                # Lost the idempotency race at the DB constraint despite
                # `record`'s SAVEPOINT (e.g. the commit itself collided).
                # The winner's equivalent work already succeeded, so this
                # is an ACK — a NACK here would ask for work that is
                # definitionally already done.
                await session.rollback()
                logger.info("duplicate_event_absorbed_on_commit")
            except Exception as exc:  # noqa: BLE001
                # Could not record the outcome. NACK: redelivery is safe
                # (process() is idempotent) and losing the ledger entry
                # silently is not.
                await session.rollback()
                logger.error("processed_event_record_failed", error=str(exc))
                message.nack()
                return

            message.ack()
            logger.info("event_processed", status=status.value)

    # ------------------------------------------------------------------
    # Pub/Sub plumbing
    # ------------------------------------------------------------------
    def _sync_callback(self, message: Any) -> None:
        """
        Runs on the client library's background thread. Its ONLY job is to
        get onto the event loop; blocking here is intentional backpressure
        (see the module docstring on FlowControl).
        """
        if self._loop is None:  # pragma: no cover - defensive
            message.nack()
            return
        future = asyncio.run_coroutine_threadsafe(self._handle(message), self._loop)
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - _handle should never raise; belt and braces
            logger.error("worker_callback_crashed", error=str(exc))
            try:
                message.nack()
            except Exception:  # noqa: BLE001, S110 - nothing further can be done
                pass

    def _build_subscriber(self) -> Any:
        if self._subscriber is None:
            import os

            if self._settings.PUBSUB_EMULATOR_HOST:
                os.environ["PUBSUB_EMULATOR_HOST"] = self._settings.PUBSUB_EMULATOR_HOST
            from google.cloud import pubsub_v1  # noqa: PLC0415 - deferred, see EventPublisher

            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    def _flow_control(self) -> Any:
        from google.cloud import pubsub_v1  # noqa: PLC0415

        return pubsub_v1.types.FlowControl(max_messages=self._settings.WORKER_CONCURRENCY)

    async def run(self) -> None:
        """
        Starts the streaming pull and blocks until SIGTERM/SIGINT.

        Shutdown order matters: cancel the pull FIRST (stop accepting new
        messages), then drain what is in flight up to
        `WORKER_SHUTDOWN_GRACE_SECONDS`. Draining first would be racing
        against a stream still delivering new work.
        """
        self._init_runtime()
        self._loop = asyncio.get_running_loop()
        self.install_signal_handlers()
        self.start_heartbeat()

        subscriber = self._build_subscriber()
        subscription_path = subscriber.subscription_path(
            self._settings.GCP_PROJECT_ID, self._subscription
        )
        self._streaming_future = subscriber.subscribe(
            subscription_path, callback=self._sync_callback, flow_control=self._flow_control()
        )

        logger.info(
            "worker_started",
            worker=self.worker_name,
            subscription=subscription_path,
            concurrency=self._settings.WORKER_CONCURRENCY,
        )

        try:
            await self._shutdown_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Cancel the pull, drain in-flight work (bounded), release resources."""
        if self._streaming_future is not None:
            self._streaming_future.cancel()

        deadline = self._shutdown_grace
        waited = 0.0
        while self._in_flight > 0 and waited < deadline:
            await asyncio.sleep(0.1)
            waited += 0.1
        if self._in_flight > 0:
            # Unacked messages are redelivered, and consumers are
            # idempotent — so this is loud but not dangerous.
            logger.warning("worker_shutdown_with_messages_in_flight", in_flight=self._in_flight)

        await self.stop_heartbeat()
        if self._subscriber is not None:
            try:
                self._subscriber.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker_subscriber_close_failed", error=str(exc))
        logger.info("worker_stopped", worker=self.worker_name, drained_seconds=round(waited, 1))


def make_correlated_child_id(envelope: EventEnvelope) -> uuid.UUID:
    """
    Helper for workers that publish follow-up events: the child's
    `causation_id` is the parent's `event_id`, which is what turns a flat
    stream of events into a traceable chain.
    """
    return envelope.event_id
