"""add optimistic-lock version column to file_metadata (Phase 4)

Revision ID: 0004_distributed
Revises: 0003_storage
Create Date: 2026-08-04 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_distributed"
down_revision: Union[str, None] = "0003_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backs SQLAlchemy's `version_id_col` optimistic-concurrency-control
    # feature on FileMetadata (see app/models/file_metadata.py). With
    # multiple stateless instances able to handle any request, two
    # instances can race to update the same file's metadata at the same
    # moment; this column is what turns that race into a clean 409
    # Conflict for the loser instead of a silent lost update.
    op.add_column(
        "file_metadata",
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("file_metadata", "lock_version")
