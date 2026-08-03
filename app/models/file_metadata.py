"""
FileMetadata ORM model.

Design decisions:
- Phase 2 manages metadata ONLY — no bytes are ever written to storage
  here. `stored_filename` reserves the future object key/name that a
  later phase's storage layer (GCS) will actually write to, so the
  eventual upload phase has a stable, pre-agreed target rather than
  inventing naming on the fly.
- `status` starts at `RESERVED` (metadata exists, no bytes yet) and will
  transition to `ACTIVE` once a future upload phase confirms the bytes
  landed in storage. This lets future phases distinguish "real, uploaded
  files" from "metadata placeholders" without touching this phase's schema.
- `version` is the file's *current* version number (starts at 1). Each
  version's own checksum/size snapshot lives in `FileVersion` — this
  table only tracks the current pointer, keeping "what is this file
  right now" a cheap single-row read instead of a join+MAX() every time.
- `checksum`/`size` mirror the current version's values here (denormalized)
  for the same reason — the file list/search views need them constantly
  and shouldn't have to join `file_versions` for every row.
- Filenames are unique **within the same folder** (partial index below,
  same pattern as Folder), not globally.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.models.mixins import AuditMixin, SoftDeleteMixin


class FileStatus(str, Enum):
    """Lifecycle status of a file metadata record."""

    RESERVED = "reserved"  # metadata created; no bytes uploaded yet (Phase 2 default)
    ACTIVE = "active"  # bytes confirmed present in storage (Phase 3 upload)
    ARCHIVED = "archived"  # retained but not part of the "live" view


class UploadStatus(str, Enum):
    """Lifecycle status of the underlying cloud storage object (Phase 3)."""

    PENDING = "pending"  # metadata row exists; upload not yet attempted/finished
    COMPLETED = "completed"  # bytes verified present in the storage backend
    FAILED = "failed"  # upload attempted and failed; object does not exist


class FileMetadata(Base, AuditMixin, SoftDeleteMixin):
    __tablename__ = "file_metadata"
    __table_args__ = (
        Index(
            "ux_file_metadata_folder_filename_active",
            "owner_id",
            "folder_id",
            "original_filename",
            unique=True,
            postgresql_where="is_deleted = false",
        ),
        Index("ix_file_metadata_owner_id", "owner_id"),
        Index("ix_file_metadata_folder_id", "folder_id"),
        Index("ix_file_metadata_extension", "extension"),
        Index("ix_file_metadata_mime_type", "mime_type"),
        Index("ix_file_metadata_checksum", "checksum"),
        Index("ix_file_metadata_upload_status", "upload_status"),
        Index("ix_file_metadata_object_name", "object_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable folder_id = file lives at the owner's top level (mirrors
    # Folder.parent_folder_id being nullable for top-level folders).
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    status: Mapped[FileStatus] = mapped_column(
        SAEnum(
            FileStatus,
            name="file_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=FileStatus.RESERVED,
        nullable=False,
        server_default=FileStatus.RESERVED.value,
    )

    # ------------------------------------------------------------------
    # Storage backend fields (Phase 3). All nullable: a RESERVED-status
    # row created by Phase 2's metadata-only API never populates these.
    # ------------------------------------------------------------------
    storage_provider: Mapped[str | None] = mapped_column(String(32), nullable=True, default="gcs")
    bucket_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Deliberately NOT unique: content-deduplicated uploads let more than
    # one row point at the same physical object (see FileUploadService).
    object_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    public_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    upload_status: Mapped[UploadStatus] = mapped_column(
        SAEnum(
            UploadStatus,
            name="upload_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UploadStatus.PENDING,
        nullable=False,
        server_default=UploadStatus.PENDING.value,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FileMetadata id={self.id} name={self.original_filename!r} v{self.version}>"