"""
Organization ORM model — the tenant boundary (Phase 13).

Design decisions
----------------
- **Quota fields live directly on the tenant row, not a side table.**
  `storage_used_bytes` is updated by the SAME atomic, guarded `UPDATE`
  that checks the limit (see `QuotaService`) — putting it on a separate
  `organization_quotas` table would only add a join to every quota
  check for no isolation or normalization benefit (an organization has
  exactly one quota, always; this is a 1:1 relationship, which is
  precisely when denormalizing onto the parent row is correct).
- **No hard delete.** `status=DELETED` is a soft marker (see
  `OrganizationStatus`'s docstring) — nothing in this phase adds a
  background job that purges a deleted organization's GCS objects/rows.
  This is a deliberate, documented gap (`docs/administration.md`
  "secure deletion"), not an oversight: a real "purge everything"
  operation is irreversible and needs its own careful design (grace
  period, export-before-delete, GCS bulk-delete backoff) that a
  multi-tenancy phase adding the CONCEPT of an organization is not the
  place to also invent.
- **`slug` is unique and used nowhere security-sensitive.** It exists
  purely as a human-friendly identifier for future URL/display use;
  every actual authorization/lookup path in this phase uses `id`
  (UUID), never `slug` — see `docs/multi-tenancy.md`.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import OrganizationStatus
from app.database.session import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)

    status: Mapped[OrganizationStatus] = mapped_column(
        SAEnum(
            OrganizationStatus,
            name="organization_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=OrganizationStatus.ACTIVE,
        nullable=False,
        server_default=OrganizationStatus.ACTIVE.value,
    )

    # Marks the auto-created, single-owner organization every user gets
    # at registration (Phase 13's backward-compatibility mechanism — see
    # `docs/multi-tenancy.md` "Migration strategy"). Never shown as a
    # distinguishing UI concept beyond "this org can't be left" — a
    # personal org's sole OWNER cannot remove themselves (see
    # `MembershipService.remove_member`), since that would leave an
    # ownerless tenant holding real files.
    is_personal: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")

    # ------------------------------------------------------------------
    # Quotas (Phase 13 §21-23). NULL limit = unlimited (the default for
    # every auto-created personal organization — see Settings.
    # ORG_DEFAULT_STORAGE_LIMIT_BYTES for the configurable default
    # applied to NEWLY created, non-personal organizations instead).
    # ------------------------------------------------------------------
    storage_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False, server_default="0")
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Organization id={self.id} slug={self.slug!r} status={self.status}>"
