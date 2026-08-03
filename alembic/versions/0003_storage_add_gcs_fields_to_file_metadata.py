"""add cloud storage fields to file_metadata

Revision ID: 0003_storage
Revises: 0002_metadata
Create Date: 2026-08-04 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_storage"
down_revision: Union[str, None] = "0002_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    upload_status_enum = postgresql.ENUM("pending", "completed", "failed", name="upload_status")
    upload_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column("file_metadata", sa.Column("storage_provider", sa.String(length=32), nullable=True, server_default="gcs"))
    op.add_column("file_metadata", sa.Column("bucket_name", sa.String(length=255), nullable=True))
    op.add_column("file_metadata", sa.Column("object_name", sa.String(length=1024), nullable=True))
    op.add_column("file_metadata", sa.Column("public_url", sa.String(length=2048), nullable=True))
    op.add_column("file_metadata", sa.Column("storage_class", sa.String(length=32), nullable=True))
    op.add_column("file_metadata", sa.Column("etag", sa.String(length=255), nullable=True))
    op.add_column("file_metadata", sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "file_metadata",
        sa.Column(
            "upload_status",
            postgresql.ENUM("pending", "completed", "failed", name="upload_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
    )

    # Deliberately NOT unique: content-deduplicated uploads let more than
    # one row point at the same physical object (see FileUploadService).
    op.create_index("ix_file_metadata_object_name", "file_metadata", ["object_name"])
    op.create_index("ix_file_metadata_checksum", "file_metadata", ["checksum"])
    op.create_index("ix_file_metadata_upload_status", "file_metadata", ["upload_status"])


def downgrade() -> None:
    op.drop_index("ix_file_metadata_upload_status", table_name="file_metadata")
    op.drop_index("ix_file_metadata_checksum", table_name="file_metadata")
    op.drop_index("ix_file_metadata_object_name", table_name="file_metadata")

    op.drop_column("file_metadata", "upload_status")
    op.drop_column("file_metadata", "uploaded_at")
    op.drop_column("file_metadata", "etag")
    op.drop_column("file_metadata", "storage_class")
    op.drop_column("file_metadata", "public_url")
    op.drop_column("file_metadata", "object_name")
    op.drop_column("file_metadata", "bucket_name")
    op.drop_column("file_metadata", "storage_provider")

    postgresql.ENUM("pending", "completed", "failed", name="upload_status").drop(op.get_bind(), checkfirst=True)
