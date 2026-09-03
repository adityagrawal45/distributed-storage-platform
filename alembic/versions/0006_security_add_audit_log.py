"""add audit_logs (Phase 10 security audit trail)

Revision ID: 0006_security
Revises: 0005_events
Create Date: 2026-09-03 00:00:00

Follows the exact shape 0004/0005 established: native enum types
created first with `checkfirst=True`, then the table (referencing
those enums with `create_type=False`), then indexes; `downgrade()`
reverses in strictly the opposite order so the migration round-trips
cleanly.

This migration has NEVER been run against a real Postgres in this
session — same honest caveat 0005's own docstring carries, verified
only via the SQLite-backed test suite and import-correctness. Run
`alembic upgrade head` / `downgrade -1` / `upgrade head` against a
real Postgres before trusting it in production.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_security"
down_revision: Union[str, None] = "0005_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUDIT_EVENT_TYPES = (
    "login_success",
    "login_failure",
    "logout",
    "token_refresh",
    "token_revocation",
    "file_download",
    "file_delete",
    "admin_action",
)
_AUDIT_RESULTS = ("success", "failure")


def upgrade() -> None:
    event_type_enum = postgresql.ENUM(*_AUDIT_EVENT_TYPES, name="audit_event_type")
    event_type_enum.create(op.get_bind(), checkfirst=True)

    result_enum = postgresql.ENUM(*_AUDIT_RESULTS, name="audit_result")
    result_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_type",
            postgresql.ENUM(*_AUDIT_EVENT_TYPES, name="audit_event_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "result",
            postgresql.ENUM(*_AUDIT_RESULTS, name="audit_result", create_type=False),
            nullable=False,
        ),
        # SET NULL, not CASCADE: deleting a user must never delete the
        # audit trail of what that user (or an admin acting on them) did.
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    postgresql.ENUM(name="audit_result").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="audit_event_type").drop(op.get_bind(), checkfirst=True)
