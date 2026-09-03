"""
AuditLog ORM model (Phase 10).

Design decisions:
- No mixins (same precedent as `OutboxEvent`/`ProcessedEvent` in Phase
  8 — see those models' own reasoning): an audit row is created once
  and never updated, so `AuditMixin`'s `updated_by`/`updated_at` would
  be meaningless columns nobody ever writes a second value into.
- No soft-delete, no update path anywhere in the repository. An audit
  trail that can be edited or hidden by the same application whose
  actions it records is not an audit trail — immutability is the
  entire point, enforced here the same way Phase 9's reconciliation
  service enforces read-only by construction (no UPDATE/DELETE
  statement exists in `AuditLogRepository`), not by a runtime flag.
- `actor_user_id` is nullable: a `LOGIN_FAILURE` for a nonexistent
  email has no user to attribute it to, and recording only `actor_email`
  in that case is what makes failed-login auditing possible at all.
- `resource_id` is a generic UUID (not a FK) because a single audit
  trail spans multiple resource types (files, users) — a FK would have
  to point at one specific table, defeating the point of one shared
  ledger. `resource_type` is the free-text discriminator.
- `detail` is JSON, deliberately small and non-sensitive by convention
  enforced at the call site, not by this model — see
  `AuditService.record`'s docstring for the never-log list.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SAEnum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AuditEventType, AuditResult
from app.database.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_user_id", "actor_user_id"),
        Index("ix_audit_logs_event_type", "event_type"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    event_type: Mapped[AuditEventType] = mapped_column(
        SAEnum(
            AuditEventType,
            name="audit_event_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    result: Mapped[AuditResult] = mapped_column(
        SAEnum(
            AuditResult,
            name="audit_result",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    # Nullable: SET NULL on the actor's deletion, deliberately, rather
    # than CASCADE — deleting a user must never delete the audit trail
    # of what that user (or an admin acting on them) did; the same
    # ON DELETE SET NULL choice AuditMixin already makes for
    # created_by/updated_by elsewhere in this codebase.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Captured alongside actor_user_id (not instead of it) so a
    # LOGIN_FAILURE against an email with no matching user is still
    # attributable to *something* for abuse investigation, and so a
    # later user-deletion doesn't erase which email a successful login
    # actually used.
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # JSONB in Postgres (queryable), falling back to plain JSON under the
    # SQLite-backed test suite — the same cross-dialect technique
    # `OutboxEvent.payload` already established in Phase 8.
    detail: Mapped[dict | None] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog id={self.id} event_type={self.event_type} result={self.result}>"
