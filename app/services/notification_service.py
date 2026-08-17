"""
Notification delivery (Phase 8).

What ships and what deliberately does not
-----------------------------------------
`NotificationSender` is a one-method abstraction; `LoggingNotificationSender`
is the only implementation Phase 8 ships. It writes a `Notification` row
and logs "would send email (stub)" at INFO. There is **no SMTP, no
SendGrid/SES/FCM, no template engine and no delivery retry** — see
`app/models/notification.py`'s docstring for why that is a scope decision
rather than an unfinished one (secrets management, bounce/complaint
handling, unsubscribe compliance and per-provider retry semantics are an
entire phase's worth of concerns, and none of them are about event
plumbing, which is what this phase is actually about).

Why an abstraction at all, if there is exactly one implementation
-----------------------------------------------------------------
Because the seam is where the *worker* stops and the *provider* starts,
and putting it in now costs one class. `NotificationWorker` knows how to
turn an event into a `Notification`; it knows nothing about how one is
delivered. Adding a real provider later is a new `NotificationSender`
subclass and one line of DI — not surgery on a consumer that also owns
idempotency, ack policy and payload parsing.

The single method is `async` even though the stub does no I/O. Every real
implementation will do network I/O, and a synchronous interface would
force the first real one to either block the worker's event loop or
change the signature (and therefore every caller and test). Paying that
now costs nothing.

Why the sender takes the session, and why it does NOT commit
------------------------------------------------------------
`LoggingNotificationSender` is constructed **per message** with the
worker's session, and only `flush()`es. `BaseWorker._handle` owns the one
commit point, so the `Notification` row and its `ProcessedEvent` row
commit together — atomically. If the sender committed on its own, a crash
between its commit and the `ProcessedEvent` write would leave a delivered
notification with no ledger entry, and the redelivery would notify the
user a second time. This is the same "one commit at the boundary"
discipline `get_db` enforces for the API.

Where the ledger write belongs once a real provider exists
-----------------------------------------------------------
Today the row is written by the sender, because the row *is* the delivery
record for this implementation. A real provider would keep the same shape
— write the row, then hand it to the provider, and record the provider's
message id on it — so the row goes on meaning "what we sent and to whom".
What would NOT work is writing the row in the worker and delivering in the
sender: then a provider failure would leave a row claiming a notification
was sent that never was.
"""

import uuid
from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.envelope import EventEnvelope
from app.exceptions.custom_exceptions import NonRetryableEventError
from app.logging.logger import get_logger
from app.models.notification import Notification

logger = get_logger(__name__)

#: Subject/body templates per `notification_type`. A dict rather than a
#: template engine: there is one notification type in Phase 8, and a
#: Jinja environment (plus its file loader, plus its sandboxing question)
#: for one string pair would be architecture for its own sake.
_TEMPLATES: dict[str, tuple[str, str]] = {
    "file_ready": (
        "Your file '{filename}' is ready",
        "'{filename}' finished processing and is now available in NimbusFS.",
    ),
}

#: Used when an event carries a `notification_type` this build has no
#: template for. Deliberately NOT an error: a newer producer introducing a
#: type before consumers know it is exactly the additive change the
#: envelope's versioning contract permits, and dropping the notification
#: (or crash-looping on it) would be a worse answer than a generic one.
_FALLBACK_TEMPLATE = (
    "NimbusFS update",
    "An update occurred on your NimbusFS account ({notification_type}).",
)


class NotificationSender(ABC):
    """
    The seam between "an event says notify this user" and "a message
    actually leaves the building".

    One method, on purpose. Anything else an implementation needs
    (credentials, a template set, a rate limiter) belongs in its
    constructor, not in this signature — a wider interface would make
    every implementation carry the union of every provider's concerns.
    """

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """
        Deliver one notification.

        Contract for implementations:
        - Raise `NonRetryableEventError` for a permanently undeliverable
          notification (a malformed address, a rejected recipient).
        - Raise anything else for a transient failure; the worker NACKs
          and Pub/Sub redelivers.
        - MUST NOT commit — the caller owns the transaction boundary.
        """


class LoggingNotificationSender(NotificationSender):
    """
    Phase 8's only sender: persist the notification, log that a real
    provider *would* have sent it.

    This is genuinely useful rather than a placeholder that does nothing:
    the row makes the whole event chain assertable in a test and
    queryable by an operator ("did the notification path actually run for
    this upload?"), which is precisely the observability a stub usually
    throws away.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def send(self, notification: Notification) -> None:
        self._session.add(notification)
        # Flush, never commit — see the module docstring. The flush is
        # what makes the row visible to reads later in this same
        # transaction (the session factory uses autoflush=False).
        await self._session.flush()

        # The event name is the literal stub message on purpose: an
        # operator grepping logs for "would send email" finds every
        # notification this build declined to actually deliver. (structlog
        # takes the event name positionally — passing `event=` as a keyword
        # as well is a TypeError, which is why it is not spelled that way.)
        logger.info(
            "would send email (stub)",
            stub=True,
            notification_id=str(notification.id),
            user_id=str(notification.user_id),
            notification_type=notification.notification_type,
            subject=notification.subject,
            related_file_id=str(notification.related_file_id) if notification.related_file_id else None,
        )


def render_notification(envelope: EventEnvelope) -> Notification:
    """
    Turns a `notification.requested` envelope into an unsaved
    `Notification`.

    Kept out of the worker so "what a notification says" is a
    notification-domain concern rather than a consumer-plumbing one, and
    kept out of the sender so every future provider renders identically.

    Raises `NonRetryableEventError` for a payload this codebase's own
    producer should never have emitted — a missing `notification_type` or
    an unparseable `file_id` is a bug, and redelivering the same bytes
    cannot fix a bug.
    """
    payload = envelope.payload or {}

    notification_type = str(payload.get("notification_type") or "").strip()
    if not notification_type:
        raise NonRetryableEventError(
            f"notification.requested {envelope.event_id} has no notification_type; nothing to render."
        )

    related_file_id: uuid.UUID | None = None
    raw_file_id = payload.get("file_id")
    if raw_file_id:
        try:
            related_file_id = uuid.UUID(str(raw_file_id))
        except ValueError as exc:
            raise NonRetryableEventError(
                f"Malformed file_id {raw_file_id!r} in notification request {envelope.event_id}."
            ) from exc

    subject_template, body_template = _TEMPLATES.get(notification_type, _FALLBACK_TEMPLATE)
    # `filename` is optional in the payload, so a template referencing it
    # must not KeyError on an event that omits it.
    fields = {
        "filename": payload.get("filename") or "your file",
        "notification_type": notification_type,
        "size": payload.get("size"),
    }

    return Notification(
        user_id=envelope.user_id,
        notification_type=notification_type,
        subject=subject_template.format(**fields)[:255],
        body=body_template.format(**fields),
        related_file_id=related_file_id,
    )
