from enum import Enum


class Role(str, Enum):
    USER = "user"
    ADMIN = "admin"


class AuditEventType(str, Enum):
    """
    Security-audit event vocabulary (Phase 10).

    Deliberately a flat, closed set rather than a free-text `action`
    string — a native Postgres enum rejects a typo'd event type at
    write time instead of letting the audit trail itself silently rot
    into inconsistent naming, the same reasoning `UserRole`/`FileStatus`
    already apply elsewhere in this codebase.

    Scoped to the events this phase actually wires up (see
    `docs/security/audit-logging.md` for the full rationale and the
    explicitly-deferred rest of the illustrative list from the Phase 10
    brief — UPLOAD_START/UPLOAD_COMPLETE/PASSWORD_CHANGE/PASSWORD_RESET
    are not emitted because the code paths they would attach to either
    don't exist yet (no password-reset feature) or would require
    touching `ChunkedUploadService`, out of scope for a first pass).
    """

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    TOKEN_REFRESH = "token_refresh"
    TOKEN_REVOCATION = "token_revocation"
    FILE_DOWNLOAD = "file_download"
    FILE_DELETE = "file_delete"
    ADMIN_ACTION = "admin_action"

    # Phase 13: multi-tenancy / sharing / administration events.
    ORGANIZATION_CREATED = "organization_created"
    ORGANIZATION_SUSPENDED = "organization_suspended"
    ORGANIZATION_REACTIVATED = "organization_reactivated"
    MEMBER_ADDED = "member_added"
    MEMBER_REMOVED = "member_removed"
    MEMBER_ROLE_CHANGED = "member_role_changed"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_REVOKED = "permission_revoked"
    SHARE_CREATED = "share_created"
    SHARE_REVOKED = "share_revoked"
    SHARE_ACCESSED = "share_accessed"
    QUOTA_CHANGED = "quota_changed"


class AuditResult(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class UploadSessionStatus(str, Enum):
    """
    Lifecycle status of a chunked/resumable upload session (Phase 6).

    See `app/core/upload_state_machine.py` for the authoritative set of
    valid transitions between these — this enum only defines the
    vocabulary, not the rules.
    """

    INITIATED = "initiated"  # session created; no chunks uploaded yet
    UPLOADING = "uploading"  # at least one chunk has landed
    COMPLETING = "completing"  # finalize in progress (composing chunks, verifying, persisting metadata)
    COMPLETED = "completed"  # terminal: FileMetadata created, bytes verified
    FAILED = "failed"  # a completion attempt failed (e.g. missing/corrupt chunk); retryable
    CANCELLED = "cancelled"  # terminal: client or operator aborted the upload
    EXPIRED = "expired"  # terminal: session outlived UPLOAD_SESSION_EXPIRATION_MINUTES


class ChunkStatus(str, Enum):
    """Lifecycle status of a single chunk within an upload session (Phase 6)."""

    PENDING = "pending"  # reserved but bytes not yet received (not currently used — rows are only created once bytes land)
    UPLOADED = "uploaded"  # bytes received and written to a temp GCS object; not yet checksum-verified
    VERIFIED = "verified"  # checksum confirmed — eligible to be included in the final compose
    FAILED = "failed"  # upload or verification failed; chunk_number remains free for retry


# ---------------------------------------------------------------------
# Phase 13 — multi-tenancy, sharing, permissions
# ---------------------------------------------------------------------
class OrganizationStatus(str, Enum):
    """
    Lifecycle status of an `Organization` (the tenant boundary — see
    `docs/multi-tenancy.md`).

    `SUSPENDED` blocks all data-plane access (uploads, downloads,
    listing) but is reversible and preserves every row — the platform-
    admin equivalent of `User.is_active=False`. `DELETED` is a soft
    marker only; nothing in this phase adds a hard-delete path for an
    organization's data (see `docs/administration.md` "secure deletion"
    for why that is explicitly deferred, not silently skipped).
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class OrganizationRole(str, Enum):
    """
    A user's role WITHIN one organization (via `OrganizationMembership`)
    — orthogonal to `UserRole` (`app/models/user.py`), which is a
    PLATFORM-wide role (regular user vs. platform administrator, Phase
    1). See `docs/authorization.md` "Two role systems, on purpose" for
    why these are not merged into one enum.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    REMOVED = "removed"


class ResourceType(str, Enum):
    """The closed set of resource kinds `ResourcePermission`/`Share` can point at."""

    FOLDER = "folder"
    FILE = "file"


class PrincipalType(str, Enum):
    """Who a `ResourcePermission` grant applies to."""

    USER = "user"
    GROUP = "group"


class Permission(str, Enum):
    """
    Resource-level capabilities (Phase 13 §11). Deliberately six, not
    dozens — see `docs/permissions.md` for what each one actually gates
    and why finer-grained splits (e.g. separating "rename" from "move")
    were rejected as complexity with no current authorization need.
    """

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    SHARE = "share"
    DOWNLOAD = "download"
    MANAGE = "manage"
