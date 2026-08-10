"""
UploadChunk ORM model (Phase 6).

Design decisions:
- One row per (upload_id, chunk_number) — enforced by a real database
  `UniqueConstraint`, not just application-level checking. This is the
  actual mechanism preventing duplicate chunk records under concurrent/
  parallel chunk uploads (see ChunkedUploadService's concurrency-model
  docstring): two requests racing to INSERT the same chunk number will
  have one succeed and one hit an `IntegrityError`, which the service
  layer catches and treats as "someone else already has (or is
  getting) this slot" rather than crashing the request.
- No `AuditMixin`/`SoftDeleteMixin`: a chunk record is an immutable,
  short-lived, high-write fact ("these bytes, at this chunk number,
  landed in storage with this checksum") — it has no meaningful
  "trashed" state (see `ChunkedUploadService.cancel_upload`, which hard-
  deletes chunk rows rather than soft-deleting them) and no
  `created_by`/`updated_by` provenance beyond "the upload session's
  owner," which is already recoverable via the `upload_id` FK. Adding
  those mixins would be schema weight with no corresponding use.
- `updated_at` uses the same **Python-side** `onupdate` callable as
  `AuditMixin` (not a server-side `func.now()`) — this mirrors the real
  bug fix documented in CONTEXT.md's History section (a server-side
  `onupdate=func.now()` caused an async `MissingGreenlet` crash on any
  request that mutated then serialized a row mid-request). A chunk row
  IS mutated in place (e.g. `status` PENDING -> VERIFIED) and then
  returned in the same request's response, so this bug would otherwise
  reproduce here exactly.
- `storage_reference` (not `object_name`, to avoid implying it's the
  FINAL object) holds the chunk's own temporary GCS object key —
  distinct from `UploadSession.storage_object`, which is the eventual
  composed destination these chunk objects get merged into and then
  deleted from under.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ChunkStatus
from app.database.session import Base


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (
        UniqueConstraint("upload_id", "chunk_number", name="uq_upload_chunk_number"),
        Index("ix_upload_chunks_upload_id", "upload_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("upload_sessions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[ChunkStatus] = mapped_column(
        SAEnum(
            ChunkStatus,
            name="upload_chunk_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=ChunkStatus.PENDING,
        nullable=False,
        server_default=ChunkStatus.PENDING.value,
    )

    storage_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),  # Python-side — see module docstring
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UploadChunk upload_id={self.upload_id} chunk_number={self.chunk_number} status={self.status.value}>"
