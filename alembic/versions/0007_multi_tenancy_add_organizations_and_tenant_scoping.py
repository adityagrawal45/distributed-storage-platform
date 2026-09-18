"""Phase 13: multi-tenancy, sharing, permissions, groups, quotas

Revision ID: 0007_multi_tenancy
Revises: 0006_security
Create Date: 2026-09-18 00:00:00

Adds the tenant boundary (`organizations`, `organization_memberships`),
groups, resource-level permission grants, secure sharing links, and
scopes every pre-existing tenant-owned table (`folders`, `file_metadata`,
`upload_sessions`, `audit_logs`) to an `organization_id`.

Backward-compatible migration strategy (Phase 13 §40)
-------------------------------------------------------
1. Create the new tables first (`organizations`, `organization_memberships`,
   `groups`, `group_memberships`, `resource_permissions`, `shares`).
2. Add `organization_id` to `folders`/`file_metadata`/`upload_sessions`
   as NULLABLE initially (a NOT NULL column cannot be added to a
   populated table in one step without a default).
3. Backfill: for every existing user, create one personal, unlimited-
   quota `Organization` (`is_personal=true`) + an OWNER
   `OrganizationMembership`, then set `organization_id` on every
   folder/file/upload_session owned by that user to their personal
   organization's id.
4. Add `organization_id` to `audit_logs` as nullable (stays nullable —
   platform-level events have no single owning tenant; see
   `AuditLog.organization_id`'s model docstring) and backfill rows that
   have a resolvable actor.
5. Alter `folders`/`file_metadata`/`upload_sessions.organization_id` to
   NOT NULL now that every row has a value, then add the FK/index/
   renamed unique constraints.

This migration has NEVER been run against a real Postgres in this
session — same honest caveat every prior migration's docstring carries
(see 0005/0006). Verified only via the SQLite-backed test suite and
import-correctness. Run `alembic upgrade head` / `downgrade -1` /
`upgrade head` against a real staging Postgres before trusting this in
production, and take a full backup first — this migration touches
every pre-existing tenant-owned table.
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_multi_tenancy"
down_revision: Union[str, None] = "0006_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORGANIZATION_STATUSES = ("active", "suspended", "deleted")
_ORGANIZATION_ROLES = ("owner", "admin", "member", "viewer")
_MEMBERSHIP_STATUSES = ("active", "removed")
_RESOURCE_TYPES = ("folder", "file")
_PRINCIPAL_TYPES = ("user", "group")
_PERMISSIONS = ("read", "write", "delete", "share", "download", "manage")

_NEW_AUDIT_EVENT_TYPES = (
    "organization_created",
    "organization_suspended",
    "organization_reactivated",
    "member_added",
    "member_removed",
    "member_role_changed",
    "permission_granted",
    "permission_revoked",
    "share_created",
    "share_revoked",
    "share_accessed",
    "quota_changed",
)
_OLD_AUDIT_EVENT_TYPES = (
    "login_success",
    "login_failure",
    "logout",
    "token_refresh",
    "token_revocation",
    "file_download",
    "file_delete",
    "admin_action",
)


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # Enums
    # ------------------------------------------------------------------
    org_status_enum = postgresql.ENUM(*_ORGANIZATION_STATUSES, name="organization_status")
    org_status_enum.create(bind, checkfirst=True)
    org_role_enum = postgresql.ENUM(*_ORGANIZATION_ROLES, name="organization_role")
    org_role_enum.create(bind, checkfirst=True)
    membership_status_enum = postgresql.ENUM(*_MEMBERSHIP_STATUSES, name="membership_status")
    membership_status_enum.create(bind, checkfirst=True)
    resource_type_enum = postgresql.ENUM(*_RESOURCE_TYPES, name="resource_type")
    resource_type_enum.create(bind, checkfirst=True)
    principal_type_enum = postgresql.ENUM(*_PRINCIPAL_TYPES, name="principal_type")
    principal_type_enum.create(bind, checkfirst=True)
    permission_enum = postgresql.ENUM(*_PERMISSIONS, name="permission")
    permission_enum.create(bind, checkfirst=True)

    # `audit_event_type` already exists (0006) — widen it in place rather
    # than drop/recreate, so existing rows survive untouched.
    for value in _NEW_AUDIT_EVENT_TYPES:
        bind.execute(sa.text(f"ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS '{value}'"))

    # ------------------------------------------------------------------
    # organizations
    # ------------------------------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_ORGANIZATION_STATUSES, name="organization_status", create_type=False),
            nullable=False,
            server_default="active",
        ),
        sa.Column("is_personal", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("storage_limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_members", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="ux_organizations_slug"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"])

    # ------------------------------------------------------------------
    # organization_memberships
    # ------------------------------------------------------------------
    op.create_table(
        "organization_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "role", postgresql.ENUM(*_ORGANIZATION_ROLES, name="organization_role", create_type=False), nullable=False
        ),
        sa.Column(
            "status",
            postgresql.ENUM(*_MEMBERSHIP_STATUSES, name="membership_status", create_type=False),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ux_org_memberships_user_org_active",
        "organization_memberships",
        ["user_id", "organization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_org_memberships_organization_id", "organization_memberships", ["organization_id"])
    op.create_index("ix_org_memberships_user_id", "organization_memberships", ["user_id"])

    # ------------------------------------------------------------------
    # groups / group_memberships
    # ------------------------------------------------------------------
    op.create_table(
        "groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ux_groups_org_name", "groups", ["organization_id", "name"], unique=True)
    op.create_index("ix_groups_organization_id", "groups", ["organization_id"])

    op.create_table(
        "group_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ux_group_memberships_group_user", "group_memberships", ["group_id", "user_id"], unique=True)
    op.create_index("ix_group_memberships_user_id", "group_memberships", ["user_id"])

    # ------------------------------------------------------------------
    # resource_permissions
    # ------------------------------------------------------------------
    op.create_table(
        "resource_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_type", postgresql.ENUM(*_RESOURCE_TYPES, name="resource_type", create_type=False), nullable=False
        ),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "principal_type",
            postgresql.ENUM(*_PRINCIPAL_TYPES, name="principal_type", create_type=False),
            nullable=False,
        ),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission", postgresql.ENUM(*_PERMISSIONS, name="permission", create_type=False), nullable=False),
        sa.Column(
            "granted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ux_resource_permissions_grant",
        "resource_permissions",
        ["resource_type", "resource_id", "principal_type", "principal_id", "permission"],
        unique=True,
    )
    op.create_index("ix_resource_permissions_resource", "resource_permissions", ["resource_type", "resource_id"])
    op.create_index("ix_resource_permissions_principal", "resource_permissions", ["principal_type", "principal_id"])
    op.create_index("ix_resource_permissions_organization_id", "resource_permissions", ["organization_id"])

    # ------------------------------------------------------------------
    # shares
    # ------------------------------------------------------------------
    op.create_table(
        "shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_type", postgresql.ENUM(*_RESOURCE_TYPES, name="resource_type", create_type=False), nullable=False
        ),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("permission", postgresql.ENUM(*_PERMISSIONS, name="permission", create_type=False), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_downloads", sa.Integer(), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_shares_token_hash", "shares", ["token_hash"], unique=True)
    op.create_index("ix_shares_resource", "shares", ["resource_type", "resource_id"])
    op.create_index("ix_shares_organization_id", "shares", ["organization_id"])
    op.create_index("ix_shares_expires_at", "shares", ["expires_at"])
    op.create_index("ix_shares_created_by", "shares", ["created_by"])

    # ------------------------------------------------------------------
    # organization_id on pre-existing tenant-owned tables — nullable first
    # ------------------------------------------------------------------
    op.add_column("folders", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("file_metadata", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("upload_sessions", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("audit_logs", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))
    # No FK, stays nullable forever (Phase 13 §34) — see OutboxEvent.organization_id's model docstring.
    op.add_column("outbox_events", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))

    # ------------------------------------------------------------------
    # Backfill: one personal organization per existing user (Phase 13 §40)
    # ------------------------------------------------------------------
    users = bind.execute(sa.text("SELECT id, first_name, last_name FROM users")).fetchall()
    for user_id, first_name, last_name in users:
        org_id = uuid.uuid4()
        display_name = f"{first_name} {last_name}".strip() or "User"
        slug = f"{display_name.lower().replace(' ', '-')[:80] or 'org'}-{uuid.uuid4().hex[:8]}"
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, name, slug, status, is_personal, storage_limit_bytes, "
                "storage_used_bytes, file_count) "
                "VALUES (:id, :name, :slug, 'active', true, NULL, 0, 0)"
            ),
            {"id": org_id, "name": f"{display_name}'s Organization", "slug": slug},
        )
        bind.execute(
            sa.text(
                "INSERT INTO organization_memberships (id, organization_id, user_id, role, status) "
                "VALUES (:id, :org_id, :user_id, 'owner', 'active')"
            ),
            {"id": uuid.uuid4(), "org_id": org_id, "user_id": user_id},
        )
        bind.execute(
            sa.text("UPDATE folders SET organization_id = :org_id WHERE owner_id = :user_id"),
            {"org_id": org_id, "user_id": user_id},
        )
        bind.execute(
            sa.text("UPDATE file_metadata SET organization_id = :org_id WHERE owner_id = :user_id"),
            {"org_id": org_id, "user_id": user_id},
        )
        bind.execute(
            sa.text("UPDATE upload_sessions SET organization_id = :org_id WHERE owner_id = :user_id"),
            {"org_id": org_id, "user_id": user_id},
        )
        bind.execute(
            sa.text("UPDATE audit_logs SET organization_id = :org_id WHERE actor_user_id = :user_id"),
            {"org_id": org_id, "user_id": user_id},
        )

    # ------------------------------------------------------------------
    # Now that every row has a value, enforce NOT NULL + FKs/indexes on
    # the three tables where organization_id is a required tenant
    # boundary. audit_logs.organization_id stays nullable by design
    # (platform-level events have no single owning tenant).
    # ------------------------------------------------------------------
    op.alter_column("folders", "organization_id", nullable=False)
    op.alter_column("file_metadata", "organization_id", nullable=False)
    op.alter_column("upload_sessions", "organization_id", nullable=False)

    op.create_foreign_key(
        "fk_folders_organization_id", "folders", "organizations", ["organization_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_file_metadata_organization_id",
        "file_metadata",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_upload_sessions_organization_id",
        "upload_sessions",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_audit_logs_organization_id", "audit_logs", "organizations", ["organization_id"], ["id"], ondelete="SET NULL"
    )

    op.create_index("ix_folders_organization_id", "folders", ["organization_id"])
    op.create_index("ix_file_metadata_organization_id", "file_metadata", ["organization_id"])
    op.create_index("ix_upload_sessions_organization_id", "upload_sessions", ["organization_id"])
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])

    # ------------------------------------------------------------------
    # Rename the owner-scoped unique constraints to organization-scoped
    # ones (Phase 13: uniqueness is a per-TENANT concept now).
    # ------------------------------------------------------------------
    op.drop_index("ux_folders_owner_parent_name_active", table_name="folders")
    op.create_index(
        "ux_folders_org_owner_parent_name_active",
        "folders",
        ["organization_id", "owner_id", "parent_folder_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.drop_index("ux_file_metadata_folder_filename_active", table_name="file_metadata")
    op.create_index(
        "ux_file_metadata_org_folder_filename_active",
        "file_metadata",
        ["organization_id", "owner_id", "folder_id", "original_filename"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index("ux_file_metadata_org_folder_filename_active", table_name="file_metadata")
    op.create_index(
        "ux_file_metadata_folder_filename_active",
        "file_metadata",
        ["owner_id", "folder_id", "original_filename"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.drop_index("ux_folders_org_owner_parent_name_active", table_name="folders")
    op.create_index(
        "ux_folders_owner_parent_name_active",
        "folders",
        ["owner_id", "parent_folder_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.drop_index("ix_audit_logs_organization_id", table_name="audit_logs")
    op.drop_index("ix_upload_sessions_organization_id", table_name="upload_sessions")
    op.drop_index("ix_file_metadata_organization_id", table_name="file_metadata")
    op.drop_index("ix_folders_organization_id", table_name="folders")

    op.drop_constraint("fk_audit_logs_organization_id", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_upload_sessions_organization_id", "upload_sessions", type_="foreignkey")
    op.drop_constraint("fk_file_metadata_organization_id", "file_metadata", type_="foreignkey")
    op.drop_constraint("fk_folders_organization_id", "folders", type_="foreignkey")

    op.drop_column("outbox_events", "organization_id")
    op.drop_column("audit_logs", "organization_id")
    op.drop_column("upload_sessions", "organization_id")
    op.drop_column("file_metadata", "organization_id")
    op.drop_column("folders", "organization_id")

    op.drop_index("ix_shares_created_by", table_name="shares")
    op.drop_index("ix_shares_expires_at", table_name="shares")
    op.drop_index("ix_shares_organization_id", table_name="shares")
    op.drop_index("ix_shares_resource", table_name="shares")
    op.drop_index("ix_shares_token_hash", table_name="shares")
    op.drop_table("shares")

    op.drop_index("ix_resource_permissions_organization_id", table_name="resource_permissions")
    op.drop_index("ix_resource_permissions_principal", table_name="resource_permissions")
    op.drop_index("ix_resource_permissions_resource", table_name="resource_permissions")
    op.drop_index("ux_resource_permissions_grant", table_name="resource_permissions")
    op.drop_table("resource_permissions")

    op.drop_index("ix_group_memberships_user_id", table_name="group_memberships")
    op.drop_index("ux_group_memberships_group_user", table_name="group_memberships")
    op.drop_table("group_memberships")

    op.drop_index("ix_groups_organization_id", table_name="groups")
    op.drop_index("ux_groups_org_name", table_name="groups")
    op.drop_table("groups")

    op.drop_index("ix_org_memberships_user_id", table_name="organization_memberships")
    op.drop_index("ix_org_memberships_organization_id", table_name="organization_memberships")
    op.drop_index("ux_org_memberships_user_org_active", table_name="organization_memberships")
    op.drop_table("organization_memberships")

    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")

    postgresql.ENUM(name="permission").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="principal_type").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="resource_type").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="membership_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="organization_role").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="organization_status").drop(op.get_bind(), checkfirst=True)

    # Note: `audit_event_type`'s new values (added via ALTER TYPE ... ADD
    # VALUE) are intentionally NOT removed here — Postgres has no
    # "remove enum value" operation, and any audit_logs row already
    # written with one of the new event types would make a value-removal
    # migration destructive. This is a documented, permanent one-way
    # widening of that enum, consistent with an audit trail's own
    # immutability principle (see AuditLog's model docstring).
