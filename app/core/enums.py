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