"""
Shared SQLAlchemy declarative mixins.

Design decisions:
- `AuditMixin` centralizes `created_by` / `updated_by` / timestamps so
  every auditable entity (Folder, FileMetadata) gets identical, consistent
  provenance tracking without copy-pasting columns. `created_by` /
  `updated_by` are nullable FKs to `users.id` with `ON DELETE SET NULL` —
  we never want deleting a user to cascade-delete files/folders they
  touched; we simply lose the "who" attribution, which is an acceptable
  trade-off versus orphaning records or blocking user deletion entirely.
- `SoftDeleteMixin` adds `is_deleted`, `deleted_at`, `deleted_by`. We use
  soft delete (a flag), not physical `DELETE`, for both Folder and
  FileMetadata so:
    1. "Trash" is simply "is_deleted = true" — no separate trash table
       needs to be kept in sync with the source tables (avoiding a
       dual-write consistency problem).
    2. "Restore" is a single UPDATE flipping the flag back.
    3. "Permanent Delete" is the only path that issues a real SQL DELETE,
       and it's a deliberate, separate operation.
- Both mixins are pure column containers (no relationships), so they can
  be combined via multiple inheritance without name collisions.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class AuditMixin:
    """created_by / updated_by / created_at / updated_at columns."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SoftDeleteMixin:
    """is_deleted / deleted_at / deleted_by columns for the soft-delete/trash pattern."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )