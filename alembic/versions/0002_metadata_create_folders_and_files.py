"""create folders, file_metadata, file_versions tables

Revision ID: 0002_metadata
Revises: 0001_initial
Create Date: 2026-07-31 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_metadata"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    file_status_enum = postgresql.ENUM("reserved", "active", "archived", name="file_status")
    file_status_enum.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # folders
    # ------------------------------------------------------------------
    op.create_table(
        "folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=4096), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_root", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_folder_id"], ["folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["deleted_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_folders_path", "folders", ["path"])
    op.create_index("ix_folders_owner_id", "folders", ["owner_id"])
    op.create_index(
        "ux_folders_owner_parent_name_active",
        "folders",
        ["owner_id", "parent_folder_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    # ------------------------------------------------------------------
    # file_metadata
    # ------------------------------------------------------------------
    op.create_table(
        "file_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=512), nullable=False),
        sa.Column("extension", sa.String(length=32), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status",
            postgresql.ENUM("reserved", "active", "archived", name="file_status", create_type=False),
            nullable=False,
            server_default="reserved",
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("stored_filename", name="ux_file_metadata_stored_filename"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["folder_id"], ["folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["deleted_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_file_metadata_owner_id", "file_metadata", ["owner_id"])
    op.create_index("ix_file_metadata_folder_id", "file_metadata", ["folder_id"])
    op.create_index("ix_file_metadata_extension", "file_metadata", ["extension"])
    op.create_index("ix_file_metadata_mime_type", "file_metadata", ["mime_type"])
    op.create_index(
        "ux_file_metadata_folder_filename_active",
        "file_metadata",
        ["owner_id", "folder_id", "original_filename"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    # ------------------------------------------------------------------
    # file_versions
    # ------------------------------------------------------------------
    op.create_table(
        "file_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("file_id", "version", name="ux_file_versions_file_id_version"),
        sa.ForeignKeyConstraint(["file_id"], ["file_metadata.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_file_versions_file_id", "file_versions", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_file_versions_file_id", table_name="file_versions")
    op.drop_table("file_versions")

    op.drop_index("ux_file_metadata_folder_filename_active", table_name="file_metadata")
    op.drop_index("ix_file_metadata_mime_type", table_name="file_metadata")
    op.drop_index("ix_file_metadata_extension", table_name="file_metadata")
    op.drop_index("ix_file_metadata_folder_id", table_name="file_metadata")
    op.drop_index("ix_file_metadata_owner_id", table_name="file_metadata")
    op.drop_table("file_metadata")

    op.drop_index("ux_folders_owner_parent_name_active", table_name="folders")
    op.drop_index("ix_folders_owner_id", table_name="folders")
    op.drop_index("ix_folders_path", table_name="folders")
    op.drop_table("folders")

    postgresql.ENUM("reserved", "active", "archived", name="file_status").drop(op.get_bind(), checkfirst=True)