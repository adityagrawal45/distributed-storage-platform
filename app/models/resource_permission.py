"""
ResourcePermission ORM model (Phase 13) — a direct grant of one
`Permission` on one resource (a `Folder` or `FileMetadata` row) to one
principal (a `User` or a `Group`).

Design decisions
----------------
- **`resource_id` is a bare UUID, not a `ForeignKey`** — same reasoning
  `AuditLog.resource_id` already established: one shared permissions
  table spans two resource types (`ResourceType.FOLDER`/`FILE`), and a
  FK can only ever point at one target table. `resource_type` is the
  discriminator; the application (never the database) is responsible
  for confirming the referenced row exists, exactly like `AuditLog`.
- **`organization_id` is denormalized onto every row**, even though
  it's technically derivable from the resource itself (a `Folder`/
  `FileMetadata` row already carries its own `organization_id`).
  Denormalizing it here is what lets `PermissionResolver` (Phase 13's
  centralized authorization service) filter grants by organization
  DIRECTLY in the same query that filters by resource — one WHERE
  clause enforces both "is this grant for the right resource" and "is
  this grant even in the caller's tenant" without a join back to
  `folders`/`file_metadata` on every authorization check, which is the
  hot path this whole model exists to make cheap (Phase 13 §37).
- **Folder grants apply to descendants via `Folder.path`, not a second
  table of resolved/expanded grants.** `PermissionResolver` walks the
  ancestor chain of a resource's folder(s) and checks THIS table for a
  grant on any ancestor — see `app/core/authorization.py`'s docstring
  for the full algorithm and precedence rules (Phase 13 §12-13).
- **Unique per (resource, principal, permission)** — granting the same
  permission twice is a no-op, not two rows; re-granting an existing
  permission is idempotent by construction (Phase 13 §32) rather than
  needing an idempotency-key mechanism bolted on top.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Permission, PrincipalType, ResourceType
from app.database.session import Base


class ResourcePermission(Base):
    __tablename__ = "resource_permissions"
    __table_args__ = (
        Index(
            "ux_resource_permissions_grant",
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            "permission",
            unique=True,
        ),
        Index("ix_resource_permissions_resource", "resource_type", "resource_id"),
        Index("ix_resource_permissions_principal", "principal_type", "principal_id"),
        Index("ix_resource_permissions_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    resource_type: Mapped[ResourceType] = mapped_column(
        SAEnum(
            ResourceType,
            name="resource_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    principal_type: Mapped[PrincipalType] = mapped_column(
        SAEnum(
            PrincipalType,
            name="principal_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    # Bare UUID, same reasoning as resource_id: it's a user_id OR a
    # group_id depending on principal_type, never both a User FK and a
    # Group FK on the same row.
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    permission: Mapped[Permission] = mapped_column(
        SAEnum(
            Permission,
            name="permission",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ResourcePermission {self.permission} on {self.resource_type}:{self.resource_id} "
            f"for {self.principal_type}:{self.principal_id}>"
        )
