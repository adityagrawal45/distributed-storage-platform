"""
FileVersion ORM model — immutable snapshot history for a file.

Design decision: rows here are append-only and never updated/deleted by
normal application flow. `FileMetadata.version/checksum/size` always
mirrors the latest row here for a given `file_id`; this table is what
powers "Version History" (`GET /metadata/{id}/versions`). Kept as its
own table (rather than e.g. a JSON column on FileMetadata) so it can grow
unbounded without bloating the frequently-read parent row, and so it can
be indexed/queried independently.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (
        UniqueConstraint("file_id", "version", name="ux_file_versions_file_id_version"),
        Index("ix_file_versions_file_id", "file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_metadata.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FileVersion file_id={self.file_id} v{self.version}>"