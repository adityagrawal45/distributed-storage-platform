"""
OrganizationMembership ORM model (Phase 13) — the User <-> Organization
many-to-many join, carrying the user's role within that one organization.

Design decisions
----------------
- **A row is never physically deleted on removal.** `status=REMOVED`
  (not a `DELETE`) preserves who was ever a member of a tenant, when,
  and with what role — load-bearing for audit ("member removed" needs
  a row to point at) and for the unique constraint below to keep doing
  its job (see next point).
- **`unique(user_id, organization_id)` is a PARTIAL index, active-only.**
  A plain unique constraint would permanently block re-adding a
  previously-removed member (the old REMOVED row would collide with a
  new ACTIVE one) — the same "partial unique index, WHERE not
  soft-deleted" pattern `Folder`/`FileMetadata` already use for their
  own name-uniqueness constraints, applied here to membership instead.
- **No `AuditMixin`/`SoftDeleteMixin`.** Those mixins' `created_by`/
  `updated_by`/`deleted_by` columns don't fit a join-row whose OWN
  identity fields (`user_id`, `organization_id`) already say who it's
  about; `removed_at`/`status` (below) are the deliberately narrower
  fields this row actually needs.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import MembershipStatus, OrganizationRole
from app.database.session import Base


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        Index(
            "ux_org_memberships_user_org_active",
            "user_id",
            "organization_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        Index("ix_org_memberships_organization_id", "organization_id"),
        Index("ix_org_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[OrganizationRole] = mapped_column(
        SAEnum(
            OrganizationRole,
            name="organization_role",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        SAEnum(
            MembershipStatus,
            name="membership_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=MembershipStatus.ACTIVE,
        nullable=False,
        server_default=MembershipStatus.ACTIVE.value,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrganizationMembership user={self.user_id} org={self.organization_id} role={self.role}>"
