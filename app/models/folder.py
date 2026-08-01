"""
Folder ORM model — a self-referential tree structure.

Design decisions:
- Self-referential FK (`parent_folder_id -> folders.id`) models unlimited
  nesting directly; there's no fixed depth limit baked into the schema.
- `path` is a **materialized path** (e.g. `/Documents/Projects/AI`),
  denormalized and kept in sync on every rename/move. This trades a bit
  of write complexity (path must be recomputed for the folder and cascaded
  to all descendants) for very cheap reads: breadcrumbs, "is this a
  descendant of X" checks, and prefix-based subtree queries
  (`WHERE path LIKE '/Documents/%'`) all become simple string operations
  instead of recursive CTEs. Given folder trees are read far more often
  than moved, this trade-off favors read performance.
- `level` (0 = top-level) is denormalized alongside `path` for the same
  reason — cheap "how deep is this folder" without walking parents.
- `is_root` is true exactly when `parent_folder_id IS NULL`. It's stored
  (not computed) purely so it can be indexed/filtered efficiently and so
  API responses don't need a NULL-check to answer "is this top-level".
- Folder names are unique **within the same parent** (see the composite
  unique index below), not globally — `/Documents/Projects` and
  `/Archive/Projects` can coexist.
- `ondelete="CASCADE"` on the self-referential FK means a *physical*
  delete of a folder cascades to children at the DB level as a safety
  net; the application-level "permanent delete" flow deletes the subtree
  explicitly and deliberately (see `FolderService`) rather than relying
  solely on this cascade, so behavior stays predictable.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.models.mixins import AuditMixin, SoftDeleteMixin


class Folder(Base, AuditMixin, SoftDeleteMixin):
    __tablename__ = "folders"
    __table_args__ = (
        # A given owner cannot have two non-deleted folders with the same
        # name under the same parent. Partial index (WHERE is_deleted =
        # false) so a soft-deleted "Reports" doesn't block creating a new
        # "Reports" in the same location.
        Index(
            "ux_folders_owner_parent_name_active",
            "owner_id",
            "parent_folder_id",
            "name",
            unique=True,
            postgresql_where="is_deleted = false",
        ),
        Index("ix_folders_path", "path"),
        Index("ix_folders_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    level: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    is_root: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Folder id={self.id} path={self.path!r}>"