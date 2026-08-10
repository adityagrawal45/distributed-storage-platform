from enum import Enum


class Role(str, Enum):
    USER = "user"
    ADMIN = "admin"


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