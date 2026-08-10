"""
UploadSession ORM model (Phase 6).

Design decisions:
- This row IS the distributed system's authoritative upload state — see
  ChunkedUploadService's module docstring for the full "Postgres is
  authoritative, GCS holds bytes, Redis only coordinates" contract.
  Whichever pod a request lands on, loading this row by `id` (+
  `owner_id` for ownership scoping) is the entire mechanism by which
  that pod "resumes" an upload it has never seen before.
- `AuditMixin` (not `SoftDeleteMixin`): an upload session's lifecycle is
  fully captured by its own `status` enum (INITIATED through EXPIRED —
  see `app/core/upload_state_machine.py`), which is a richer, more
  precise model than a boolean soft-delete flag. Reusing
  `SoftDeleteMixin` here would conflate "abandoned/expired" (routine,
  expected) with "trashed" (a user-initiated undo-able action on a
  *file*) — two different concepts that happen to sound similar.
- `storage_object` is generated once, at INITIATE time (via the same
  `StorageService.generate_object_name` Phase 3 already uses), and never
  changes — it's the eventual FINAL object's key. Individual chunks
  land at their own temporary, derived object names (see
  ChunkedUploadService); this column is not where chunk bytes go.
- `gcs_upload_id` is reserved but unused by the default chunk-temp-
  object + Compose path this phase implements (see ChunkedUploadService
  module docstring for why: a single GCS resumable session is
  sequential-only and can't satisfy the "parallel chunk upload"
  requirement). It's kept as a nullable column so a future, simpler
  "small-file, no-parallelism-needed" fallback path could populate it
  without a schema migration — not wired up this phase.
- `uploaded_bytes` is NOT maintained via concurrent read-modify-write
  increments from parallel chunk uploads (that's a classic lost-update
  race under concurrency — see ChunkedUploadService's concurrency-model
  docstring). It's written exactly once, atomically, at completion. Live
  progress (`GET /uploads/{id}`) is instead computed by a fresh
  `SUM(size)` aggregate over verified `UploadChunk` rows every time it's
  requested — slower per-call, but race-free by construction, which
  matters far more here.
- No relationship() attributes are declared (mirrors `FileVersion`'s
  plain-FK style) — this phase's repositories query explicitly via
  `select()`, never lazy-loaded ORM traversal, keeping async session
  usage predictable (no accidental sync I/O from lazy-loading).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import UploadSessionStatus
from app.database.session import Base
from app.models.mixins import AuditMixin


class UploadSession(Base, AuditMixin):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        Index("ix_upload_sessions_owner_id", "owner_id"),
        Index("ix_upload_sessions_status", "status"),
        Index("ix_upload_sessions_owner_status", "owner_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    # Populated only once the upload reaches COMPLETED — the FileMetadata
    # row this session ultimately produced.
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_metadata.id", ondelete="SET NULL"), nullable=True
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    status: Mapped[UploadSessionStatus] = mapped_column(
        SAEnum(
            UploadSessionStatus,
            name="upload_session_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UploadSessionStatus.INITIATED,
        nullable=False,
        server_default=UploadSessionStatus.INITIATED.value,
    )

    storage_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The FINAL destination object key, reserved at initiate time — see
    # module docstring. Never null; generated before the row is created.
    storage_object: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Reserved, unused by this phase's default path — see module docstring.
    gcs_upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    checksum_algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="sha256", server_default="sha256")
    expected_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actual_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # The Idempotency-Key the client supplied at INITIATE, if any —
    # stored for audit/debugging visibility; the actual replay logic
    # lives in Redis via IdempotencyService (see ChunkedUploadService),
    # this column is not itself consulted for idempotency decisions.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UploadSession id={self.id} status={self.status.value} filename={self.filename!r}>"
