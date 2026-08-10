"""add upload_sessions and upload_chunks tables (chunked/resumable uploads)

Revision ID: 0004_chunked_uploads
Revises: 0003_storage
Create Date: 2026-08-09 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_chunked_uploads"
down_revision: Union[str, None] = "0003_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SESSION_STATUSES = ("initiated", "uploading", "completing", "completed", "failed", "cancelled", "expired")
_CHUNK_STATUSES = ("pending", "uploaded", "verified", "failed")


def upgrade() -> None:
    upload_session_status_enum = postgresql.ENUM(*_SESSION_STATUSES, name="upload_session_status")
    upload_session_status_enum.create(op.get_bind(), checkfirst=True)

    upload_chunk_status_enum = postgresql.ENUM(*_CHUNK_STATUSES, name="upload_chunk_status")
    upload_chunk_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "upload_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("file_metadata.id", ondelete="SET NULL"), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            postgresql.ENUM(*_SESSION_STATUSES, name="upload_session_status", create_type=False),
            nullable=False,
            server_default="initiated",
        ),
        sa.Column("storage_bucket", sa.String(length=255), nullable=True),
        sa.Column("storage_object", sa.String(length=1024), nullable=False),
        sa.Column("gcs_upload_id", sa.String(length=255), nullable=True),
        sa.Column("checksum_algorithm", sa.String(length=32), nullable=False, server_default="sha256"),
        sa.Column("expected_checksum", sa.String(length=128), nullable=True),
        sa.Column("actual_checksum", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_upload_sessions_owner_id", "upload_sessions", ["owner_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index("ix_upload_sessions_owner_status", "upload_sessions", ["owner_id", "status"])

    op.create_table(
        "upload_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("upload_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_number", sa.Integer(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*_CHUNK_STATUSES, name="upload_chunk_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("storage_reference", sa.String(length=1024), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("upload_id", "chunk_number", name="uq_upload_chunk_number"),
    )
    op.create_index("ix_upload_chunks_upload_id", "upload_chunks", ["upload_id"])


def downgrade() -> None:
    op.drop_index("ix_upload_chunks_upload_id", table_name="upload_chunks")
    op.drop_table("upload_chunks")

    op.drop_index("ix_upload_sessions_owner_status", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_status", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_owner_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")

    postgresql.ENUM(*_CHUNK_STATUSES, name="upload_chunk_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*_SESSION_STATUSES, name="upload_session_status").drop(op.get_bind(), checkfirst=True)
