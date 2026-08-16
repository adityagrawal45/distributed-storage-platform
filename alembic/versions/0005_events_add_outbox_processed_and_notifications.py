"""add outbox_events, processed_events, notifications + file_metadata.thumbnail_object_name

Revision ID: 0005_events
Revises: 0004_chunked_uploads
Create Date: 2026-08-17 00:00:00

Phase 8 (event-driven architecture). Follows the exact shape of
`0004_chunked_uploads`: native enum types created first with
`checkfirst=True`, then tables (referencing those enums with
`create_type=False` so the table DDL doesn't try to create them a second
time), then indexes; `downgrade()` reverses in strictly the opposite
order so the migration round-trips cleanly.

One additive column ships in the same revision rather than its own:
`file_metadata.thumbnail_object_name` is nullable with no default and no
backfill, so it is a metadata-only `ALTER TABLE` in Postgres 11+ (no
table rewrite, no long lock) and is safe to apply to a live table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_events"
down_revision: Union[str, None] = "0004_chunked_uploads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OUTBOX_STATUSES = ("pending", "published", "failed")
_PROCESSED_STATUSES = ("succeeded", "failed")


def upgrade() -> None:
    outbox_status_enum = postgresql.ENUM(*_OUTBOX_STATUSES, name="outbox_event_status")
    outbox_status_enum.create(op.get_bind(), checkfirst=True)

    processed_status_enum = postgresql.ENUM(*_PROCESSED_STATUSES, name="processed_event_status")
    processed_status_enum.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # outbox_events — the transactional outbox
    # ------------------------------------------------------------------
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Unique: this is the consumer-side idempotency key.
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        # Plain string, not an enum — the event catalog grows every phase
        # and an enum would make each addition a locking migration.
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregate_type", sa.String(length=50), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # JSONB, not JSON/TEXT: queryable during an incident and stored
        # pre-parsed for replay tooling.
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_OUTBOX_STATUSES, name="outbox_event_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    # The publisher's hot polling query, in one index:
    #   WHERE status IN (...) AND next_attempt_at <= now() ORDER BY created_at
    op.create_index("ix_outbox_events_status_next_attempt", "outbox_events", ["status", "next_attempt_at"])
    op.create_index("ix_outbox_events_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])

    # ------------------------------------------------------------------
    # processed_events — consumer-side idempotency ledger
    # ------------------------------------------------------------------
    op.create_table(
        "processed_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_name", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_PROCESSED_STATUSES, name="processed_event_status", create_type=False),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # THE idempotency guarantee. The consumer's pre-check SELECT is
        # only an optimization; this constraint is what actually holds
        # when two replicas race.
        sa.UniqueConstraint("event_id", "consumer_name", name="uq_processed_events_event_consumer"),
    )
    op.create_index("ix_processed_events_event_id", "processed_events", ["event_id"])
    op.create_index("ix_processed_events_consumer", "processed_events", ["consumer_name"])

    # ------------------------------------------------------------------
    # notifications — append-only stub delivery ledger (no real provider
    # this phase; see app/models/notification.py's docstring)
    # ------------------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=100), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # Deliberately NO foreign key: a notification is an immutable
        # historical record and must survive the file it refers to.
        sa.Column("related_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])

    # ------------------------------------------------------------------
    # Additive column on the existing file_metadata table
    # ------------------------------------------------------------------
    op.add_column(
        "file_metadata",
        sa.Column("thumbnail_object_name", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_metadata", "thumbnail_object_name")

    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_processed_events_consumer", table_name="processed_events")
    op.drop_index("ix_processed_events_event_id", table_name="processed_events")
    op.drop_table("processed_events")

    op.drop_index("ix_outbox_events_status", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_next_attempt", table_name="outbox_events")
    op.drop_table("outbox_events")

    postgresql.ENUM(*_PROCESSED_STATUSES, name="processed_event_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*_OUTBOX_STATUSES, name="outbox_event_status").drop(op.get_bind(), checkfirst=True)
