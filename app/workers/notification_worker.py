"""
Notification worker — the terminal consumer in the chain.

Run with: `python -m app.workers.notification_worker`

Consumes `notification.requested` off NOTIFICATION_EVENTS_TOPIC
(published directly by the file-processing worker) and hands it to a
`NotificationSender`. Phase 8's sender writes a `Notification` row and
logs "would send email (stub)" — see `app/services/notification_service.py`
for why there is no real provider and why the seam exists anyway.

Why notifications get their own topic AND their own worker
----------------------------------------------------------
Egress isolation. A notification is the one thing in this system that
talks to a *third party*, and third parties go down independently of
anything NimbusFS operates. If notification delivery shared a
subscription with file processing, a wedged email provider would stall
the shared subscription's backlog and stop thumbnails and file validation
too — a vendor outage becoming a platform outage. Separate topic,
separate subscription, separate deployment: a notification backlog is
just a notification backlog.

It is also the cheapest worker by a wide margin (no GCS, no image decode,
no large payloads), so its Kubernetes resource request is a fraction of
the thumbnail worker's. Sharing a process would have forced the higher
number on both.

Why the sender is constructed per message
-----------------------------------------
`LoggingNotificationSender` holds the *message's* session, and
`BaseWorker` opens one session per message. Constructing the sender once
at worker startup would mean caching a session across messages — the
classic way to leak a transaction from a failed message into the next
one's work. The factory indirection (`sender_factory`) keeps that
per-message construction injectable for tests and for the future real
provider.
"""

import asyncio
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.events.envelope import EventEnvelope, EventType
from app.logging.logger import get_logger
from app.services.notification_service import (
    LoggingNotificationSender,
    NotificationSender,
    render_notification,
)
from app.workers.base import BaseWorker

logger = get_logger(__name__)

WORKER_NAME = "notification-worker"


class NotificationWorker(BaseWorker):
    worker_name = WORKER_NAME
    consumer_name = WORKER_NAME

    def __init__(
        self,
        *,
        sender_factory: Callable[[AsyncSession], NotificationSender] | None = None,
        **kwargs,
    ):
        # Defaults to the stub sender; a real provider becomes a one-line
        # change here (or an injected factory) rather than a rewrite.
        self._sender_factory = sender_factory or LoggingNotificationSender
        super().__init__(**kwargs)

    def default_subscription(self) -> str:
        return get_settings().NOTIFICATION_WORKER_SUBSCRIPTION

    def interested_in(self, envelope: EventEnvelope) -> bool:
        """
        NOTIFICATION_EVENTS_TOPIC carries only `notification.requested`
        today, but the check is explicit anyway: a topic's contents are a
        deployment-time fact, and a consumer that silently processes
        whatever arrives is one Terraform change away from doing
        something surprising.
        """
        return envelope.event_type == EventType.NOTIFICATION_REQUESTED

    async def process(self, envelope: EventEnvelope, session: AsyncSession) -> None:
        # Rendering validates the payload; a bad one raises
        # `NonRetryableEventError` and `BaseWorker` ACKs it with a FAILED
        # ledger row rather than retrying bytes that cannot improve.
        notification = render_notification(envelope)

        await self._sender_factory(session).send(notification)

        logger.info(
            "notification_recorded",
            notification_type=notification.notification_type,
            user_id=str(notification.user_id),
            related_file_id=str(notification.related_file_id) if notification.related_file_id else None,
        )


def main() -> None:  # pragma: no cover - process entrypoint
    from app.logging.logger import configure_logging

    configure_logging()
    asyncio.run(NotificationWorker().run())


if __name__ == "__main__":  # pragma: no cover
    main()
