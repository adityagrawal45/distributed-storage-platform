"""
Group ORM model (Phase 13) — an organization-scoped collection of users
that permissions can be granted to as a unit (`ResourcePermission`
with `principal_type=GROUP`), instead of one grant per user.

Deliberately flat: no nested groups (a `Group` cannot contain another
`Group`), no cross-organization groups. Both are real feature requests
a bigger platform eventually gets, and both are exactly the kind of
"unnecessary permission complexity" Phase 13 §10 explicitly warns
against building before there's a concrete requirement for it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        Index("ux_groups_org_name", "organization_id", "name", unique=True),
        Index("ix_groups_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Group id={self.id} org={self.organization_id} name={self.name!r}>"


class GroupMembership(Base):
    """User <-> Group join. Physically deleted on removal (unlike
    `OrganizationMembership`) — a group membership carries no role or
    history worth preserving on its own; removing someone from a group
    is not itself an event `docs/audit-logging.md`-style systems need
    to reconstruct later, only organization membership/role changes are
    (see Phase 13 §26's event list, which names member/role/permission/
    share events but not "group membership changed")."""

    __tablename__ = "group_memberships"
    __table_args__ = (
        Index("ux_group_memberships_group_user", "group_id", "user_id", unique=True),
        Index("ix_group_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GroupMembership group={self.group_id} user={self.user_id}>"
