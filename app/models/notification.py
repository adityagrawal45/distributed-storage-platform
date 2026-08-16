"""
Notification ORM model (Phase 8).

What this is — and, more importantly, what it is not
----------------------------------------------------
Phase 8 ships **no real email/SMS/push integration**. The notification
worker's `LoggingNotificationSender` writes one row here and logs
"would send email (stub)" at INFO. That is a deliberate scope decision,
not an unfinished one: wiring a real provider (SendGrid, SES, FCM) means
secrets management, bounce/complaint handling, unsubscribe compliance and
per-provider retry semantics — an entire phase's worth of concerns that
would swamp the actual subject of this phase, which is the event
plumbing.

What the table buys today is real, though: it makes the notification path
**observable and assertable**. A test can prove an event reached the
notification worker by querying for the row, and an operator can see that
a notification *would* have been sent, addressed to whom, and about
which file. When a provider is added, `NotificationSender` gains a second
implementation and this row becomes the delivery ledger it already looks
like — no schema change required.

No mixins, matching `UploadChunk`/`OutboxEvent`/`ProcessedEvent`:
append-only, high-write, no soft-delete concept. A notification is a
historical fact; you do not edit or trash one.

`related_file_id` is nullable and carries **no foreign key** on purpose.
A notification is an immutable record of something that happened; if the
file is later permanently deleted, the notification should survive
saying what it said, not cascade away or block the delete. An FK with
`ON DELETE SET NULL` would silently rewrite history instead.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # "Show me this user's notifications, newest first" — the only
        # read pattern a future in-app notification feed would need.
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Free-form string rather than an enum: notification types are
    # expected to proliferate rapidly and independently of releases, and
    # an enum would make each new one a migration holding a lock.
    notification_type: Mapped[str] = mapped_column(String(100), nullable=False)

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    related_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification user_id={self.user_id} type={self.notification_type!r}>"
