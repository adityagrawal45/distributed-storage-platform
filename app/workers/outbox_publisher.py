"""
Outbox publisher worker — moves rows from Postgres to Pub/Sub.

Run with: `python -m app.workers.outbox_publisher`

Why this is its OWN worker
--------------------------
It could have been folded into the file-processing worker, or run as an
asyncio task inside the API. It is neither, for three separate reasons,
any one of which would be sufficient:

1. **IAM.** This process needs `roles/pubsub.publisher` and nothing else
   — no subscribe, no GCS. Every other worker needs subscribe and not
   publish-to-every-topic. Merging them merges their permission sets, and
   a least-privilege service account is the cheapest security control
   available here.
2. **Failure mode.** Its failure mode is "Postgres polling stalled",
   which looks nothing like a consumer's "messages are being redelivered"
   and wants different alerts, different metrics and different runbook
   steps.
3. **Scaling.** It is a database poller, and the correct replica count
   for a poller is *low* (one is fine; `SKIP LOCKED` makes more safe but
   not obviously useful). Consumers scale with backlog. Coupling them
   would force one number on two workloads.

Running it inside the API was never an option: the API is scaled by HTTP
traffic and its replicas come and go with the HPA, so an in-process
publisher would give you N publishers where N is whatever the autoscaler
felt like, and would tie event delivery to request-serving health.

Delivery semantics
------------------
`_publish_one` publishes, then marks the row PUBLISHED, then commits —
and the commit is **per row**, not per batch. A batched commit would mean
one poisoned row rolls back the successful publishes of its siblings,
which would then be republished next poll: correct (consumers are
idempotent) but wasteful and confusing in the logs.

The residual window is deliberate and documented: if the process dies
after Pub/Sub accepted the message but before the commit, the row stays
PENDING and is republished on the next poll. That is at-least-once, which
is exactly what the whole design assumes — `ProcessedEvent`'s unique
constraint absorbs the duplicate on the consumer side. The alternative
(mark published *before* publishing) would trade a harmless duplicate for
a silently lost event, which is strictly worse.
"""

import asyncio
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.metrics import WORKER_JOBS_TOTAL, safe_call
from app.database.session import AsyncSessionLocal
from app.events.envelope import EventEnvelope, EventType
from app.events.publisher import EventPublisher
from app.logging.logger import get_logger
from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.workers.runtime import WorkerRuntimeMixin

logger = get_logger(__name__)

WORKER_NAME = "outbox-publisher"


@dataclass
class PollResult:
    """One poll iteration's outcome — returned so tests can assert on it precisely."""

    fetched: int = 0
    published: int = 0
    failed: int = 0


class OutboxPublisherWorker(WorkerRuntimeMixin):
    worker_name = WORKER_NAME

    def __init__(
        self,
        *,
        publisher: EventPublisher | None = None,
        session_factory=None,
    ):
        self._settings = get_settings()
        self._publisher = publisher or EventPublisher()
        # Injectable so tests drive the worker against the in-memory
        # SQLite session factory instead of the module-level Postgres one.
        self._session_factory = session_factory or AsyncSessionLocal
        self._init_runtime()

    # ------------------------------------------------------------------
    # One row
    # ------------------------------------------------------------------
    @staticmethod
    def _to_envelope(event: OutboxEvent) -> EventEnvelope:
        """
        Reconstructs the wire envelope from a stored row.

        `event_id` and `correlation_id` come from the row, NOT freshly
        generated — republishing must reuse the same `event_id` or
        consumer-side deduplication would never fire.
        """
        return EventEnvelope(
            event_id=event.event_id,
            event_type=EventType(event.event_type),
            event_version=event.event_version,
            occurred_at=event.created_at,
            producer="api",
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            user_id=event.user_id,
            payload=event.payload or {},
        )

    async def _publish_one(self, repo: OutboxRepository, event: OutboxEvent, session) -> bool:
        """
        Publishes one row and commits its outcome. Returns True on success.

        Every exception is caught: a single bad row (an event type with no
        topic mapping, an unserializable payload, a transient Pub/Sub
        outage) must never stop the loop or take its siblings down. It is
        recorded with `mark_failed`, which applies exponential backoff, so
        a systemic outage does not turn into a retry storm.
        """
        try:
            envelope = self._to_envelope(event)
            message_id = await self._publisher.publish(envelope)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning(
                "outbox_publish_failed",
                event_id=str(event.event_id),
                event_type=event.event_type,
                attempt=(event.attempt_count or 0) + 1,
                error=str(exc),
            )
            await repo.mark_failed(event, str(exc))
            await session.commit()
            return False

        await repo.mark_published(event)
        # Per-row commit. If the process dies right here, the row stays
        # PENDING and is republished — harmless, because consumers
        # deduplicate on `event_id`.
        await session.commit()
        logger.info(
            "outbox_row_published",
            event_id=str(event.event_id),
            event_type=event.event_type,
            message_id=message_id,
        )
        return True

    # ------------------------------------------------------------------
    # One poll
    # ------------------------------------------------------------------
    async def poll_once(self) -> PollResult:
        """
        One complete polling iteration. Public and independently
        awaitable so the whole publishing behavior is testable without
        starting the infinite loop or any signal handling.
        """
        result = PollResult()
        async with self._session_factory() as session:
            repo = OutboxRepository(session)
            batch = await repo.fetch_pending_batch(self._settings.OUTBOX_BATCH_SIZE)
            result.fetched = len(batch)

            for event in batch:
                if self.shutting_down:
                    logger.info("outbox_poll_interrupted_by_shutdown", remaining=result.fetched - result.published)
                    break
                if await self._publish_one(repo, event, session):
                    result.published += 1
                    safe_call(
                        lambda: WORKER_JOBS_TOTAL.labels(worker=WORKER_NAME, result="succeeded").inc(),
                        operation="worker_jobs_total_inc",
                    )
                else:
                    result.failed += 1
                    safe_call(
                        lambda: WORKER_JOBS_TOTAL.labels(worker=WORKER_NAME, result="failed").inc(),
                        operation="worker_jobs_total_inc",
                    )

        if result.fetched:
            logger.info(
                "outbox_poll_completed",
                fetched=result.fetched,
                published=result.published,
                failed=result.failed,
            )
        return result

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        self._init_runtime()
        self.install_signal_handlers()
        self.start_heartbeat()

        logger.info(
            "outbox_publisher_started",
            poll_interval=self._settings.OUTBOX_POLL_INTERVAL,
            batch_size=self._settings.OUTBOX_BATCH_SIZE,
            pubsub_enabled=self._publisher.enabled,
        )

        try:
            while not self.shutting_down:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001
                    # The LOOP must survive anything — including Postgres
                    # being unreachable. Crashing here would just hand the
                    # restart to Kubernetes with no backoff advantage and
                    # a noisier signal than a logged, retried poll.
                    logger.error("outbox_poll_failed", error=str(exc))
                await self.sleep_or_shutdown(self._settings.OUTBOX_POLL_INTERVAL)
        finally:
            await self.stop_heartbeat()
            logger.info("outbox_publisher_stopped")


def main() -> None:  # pragma: no cover - process entrypoint
    from app.logging.logger import configure_logging

    configure_logging()
    asyncio.run(OutboxPublisherWorker().run())


if __name__ == "__main__":  # pragma: no cover
    main()
