"""
Domain-level exceptions.

Design decision: these exceptions carry no knowledge of HTTP status codes
or FastAPI. They represent things that went wrong in the domain/application
layer. The API layer (global exception handler) is solely responsible for
translating them into HTTP responses. This keeps services/repositories
framework-agnostic and testable in isolation.
"""


class NimbusFSException(Exception):
    """Base class for all application-specific exceptions."""

    def __init__(self, detail: str = "An application error occurred."):
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------
class AuthenticationException(NimbusFSException):
    """Raised when credentials are invalid or a token cannot be trusted."""

    def __init__(self, detail: str = "Authentication failed."):
        super().__init__(detail)


class InvalidCredentialsException(AuthenticationException):
    def __init__(self, detail: str = "Incorrect email or password."):
        super().__init__(detail)


class InvalidTokenException(AuthenticationException):
    def __init__(self, detail: str = "Invalid authentication token."):
        super().__init__(detail)


class TokenExpiredException(AuthenticationException):
    def __init__(self, detail: str = "Authentication token has expired."):
        super().__init__(detail)


class InactiveUserException(AuthenticationException):
    def __init__(self, detail: str = "User account is inactive."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------
class AuthorizationException(NimbusFSException):
    """Raised when an authenticated user lacks permission for an action."""

    def __init__(self, detail: str = "You do not have permission to perform this action."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Resource / Conflict
# ---------------------------------------------------------------------
class NotFoundException(NimbusFSException):
    def __init__(self, detail: str = "Resource not found."):
        super().__init__(detail)


class ConflictException(NimbusFSException):
    def __init__(self, detail: str = "Resource already exists."):
        super().__init__(detail)


class EmailAlreadyExistsException(ConflictException):
    def __init__(self, detail: str = "An account with this email already exists."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------
class DatabaseConnectionException(NimbusFSException):
    def __init__(self, detail: str = "Database connection error."):
        super().__init__(detail)


class RedisConnectionException(NimbusFSException):
    def __init__(self, detail: str = "Redis connection error."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Metadata Management (Phase 2): Folders & Files
# ---------------------------------------------------------------------
class FolderNotFoundException(NotFoundException):
    def __init__(self, detail: str = "Folder not found."):
        super().__init__(detail)


class FileNotFoundException(NotFoundException):
    def __init__(self, detail: str = "File not found."):
        super().__init__(detail)


class DuplicateFolderException(ConflictException):
    def __init__(self, detail: str = "A folder with this name already exists in this location."):
        super().__init__(detail)


class DuplicateFileException(ConflictException):
    def __init__(self, detail: str = "A file with this name already exists in this location."):
        super().__init__(detail)


class InvalidMoveException(NimbusFSException):
    def __init__(self, detail: str = "This move operation is not allowed."):
        super().__init__(detail)


class CircularReferenceException(InvalidMoveException):
    def __init__(self, detail: str = "Cannot move a folder into itself or one of its own descendants."):
        super().__init__(detail)


class TrashException(NimbusFSException):
    def __init__(self, detail: str = "This item is not in the trash."):
        super().__init__(detail)


class ValidationException(NimbusFSException):
    def __init__(self, detail: str = "Validation failed."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Cloud Storage (Phase 3)
# ---------------------------------------------------------------------
class StorageException(NimbusFSException):
    """Base class for all Google Cloud Storage integration failures."""

    def __init__(self, detail: str = "A storage backend error occurred."):
        super().__init__(detail)


class BucketNotFoundException(StorageException):
    def __init__(self, detail: str = "The configured storage bucket does not exist."):
        super().__init__(detail)


class StorageObjectNotFoundException(StorageException):
    def __init__(self, detail: str = "The requested file's bytes could not be located in storage."):
        super().__init__(detail)


class StoragePermissionException(StorageException):
    def __init__(self, detail: str = "Permission denied by the storage backend."):
        super().__init__(detail)


class StorageTimeoutException(StorageException):
    def __init__(self, detail: str = "The storage backend timed out."):
        super().__init__(detail)


class UploadFailedException(StorageException):
    def __init__(self, detail: str = "File upload to storage failed."):
        super().__init__(detail)


class DownloadFailedException(StorageException):
    def __init__(self, detail: str = "File download from storage failed."):
        super().__init__(detail)


class ChecksumMismatchException(StorageException):
    def __init__(self, detail: str = "Uploaded content failed integrity verification (checksum mismatch)."):
        super().__init__(detail)


class RollbackFailedException(StorageException):
    """
    Raised when an upload/metadata rollback itself fails, leaving an
    orphaned object or row. Never silently swallowed — see
    FileUploadService design decisions.
    """

    def __init__(self, detail: str = "Failed to roll back a partially completed operation."):
        super().__init__(detail)


class FileTooLargeException(ValidationException):
    def __init__(self, detail: str = "File exceeds the maximum allowed upload size."):
        super().__init__(detail)


class UnsupportedFileTypeException(ValidationException):
    def __init__(self, detail: str = "This file type is not allowed."):
        super().__init__(detail)


class DuplicateFileContentException(ConflictException):
    """
    Not an error condition per se — raised by callers that want a hard
    "reject exact re-uploads" policy. FileUploadService's default
    duplicate-detection behavior instead transparently dedupes storage
    bytes and still succeeds (see its docstring), so this is only used
    where a caller explicitly opts into strict duplicate rejection.
    """

    def __init__(self, detail: str = "This exact file content has already been uploaded."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Distributed Backend (Phase 4)
# ---------------------------------------------------------------------
class LockAcquisitionException(NimbusFSException):
    """Raised when a distributed (Redis-backed) lock cannot be acquired."""

    def __init__(self, detail: str = "Could not acquire the required distributed lock."):
        super().__init__(detail)


class CircuitBreakerOpenException(NimbusFSException):
    """Raised when a call is short-circuited because its breaker is open."""

    def __init__(self, detail: str = "This dependency is temporarily unavailable (circuit breaker open)."):
        super().__init__(detail)


class ServiceUnavailableException(NimbusFSException):
    """Raised when a critical dependency (DB/Redis/Storage) cannot be reached."""

    def __init__(self, detail: str = "A required dependency is currently unavailable."):
        super().__init__(detail)


class IdempotencyKeyReplayedException(NimbusFSException):
    """
    Raised when an `Idempotency-Key` is reused with a *different* request
    body than the original — a client bug (or misuse), not a safe retry.
    Safe retries (identical key + identical body) are handled by
    replaying the cached response instead of raising.
    """

    def __init__(self, detail: str = "This Idempotency-Key was already used with a different request body."):
        super().__init__(detail)


class IdempotencyKeyInProgressException(ConflictException):
    """
    Raised when a request reuses an `Idempotency-Key` whose original
    request is still being processed by (possibly) another replica —
    the safe response is to reject the concurrent duplicate rather than
    let two replicas both execute the same non-idempotent side effect.
    """

    def __init__(self, detail: str = "A request with this Idempotency-Key is already being processed."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Chunked / Resumable Uploads (Phase 6)
# ---------------------------------------------------------------------
# Design note: every exception below deliberately subclasses an
# ALREADY-REGISTERED base (NotFoundException, ConflictException,
# ValidationException, NimbusFSException) rather than introducing new
# ones. FastAPI/Starlette resolve exception handlers by walking the
# raised type's MRO for the closest REGISTERED ancestor (see
# app/main.py's registration order), so these get correct HTTP mapping
# for free with zero new handler functions and zero main.py changes —
# the same technique Phase 2's FolderNotFoundException/
# DuplicateFolderException already relied on.
class UploadSessionNotFoundException(NotFoundException):
    def __init__(self, detail: str = "Upload session not found."):
        super().__init__(detail)


class ChunkNotFoundException(NotFoundException):
    def __init__(self, detail: str = "Chunk not found."):
        super().__init__(detail)


class InvalidUploadStateTransitionException(NimbusFSException):
    """Raised by `UploadStateMachine` when a requested state change isn't a valid transition."""

    def __init__(self, detail: str = "This upload session cannot transition to the requested state."):
        super().__init__(detail)


class UploadSessionExpiredException(ConflictException):
    def __init__(self, detail: str = "This upload session has expired."):
        super().__init__(detail)


class UploadAlreadyFinalizedException(ConflictException):
    def __init__(self, detail: str = "This upload has already been completed."):
        super().__init__(detail)


class DuplicateChunkException(ConflictException):
    """
    Raised only for the narrow case of two genuinely concurrent requests
    racing to write the SAME chunk number with DIFFERENT content — see
    ChunkedUploadService. A retry of the SAME chunk number with
    IDENTICAL content is treated as a safe no-op, not an error.
    """

    def __init__(self, detail: str = "This chunk is currently being written by another request."):
        super().__init__(detail)


class UploadIncompleteException(ValidationException):
    def __init__(self, detail: str = "Cannot complete upload: one or more chunks are missing."):
        super().__init__(detail)


class ChunkSizeInvalidException(ValidationException):
    def __init__(self, detail: str = "Chunk size is invalid for this upload session."):
        super().__init__(detail)


class ChunkNumberInvalidException(ValidationException):
    def __init__(self, detail: str = "Chunk number is out of range for this upload session."):
        super().__init__(detail)


class ChunkChecksumMismatchException(ValidationException):
    """A single chunk's content didn't match its declared checksum — a client/network data-integrity failure, not a storage backend fault."""

    def __init__(self, detail: str = "Chunk content failed checksum verification."):
        super().__init__(detail)


class FinalChecksumMismatchException(ValidationException):
    """The fully reassembled object didn't match the checksum declared at initiate time."""

    def __init__(self, detail: str = "Reassembled file failed final checksum verification."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Distributed Caching & Coordination (Phase 7)
# ---------------------------------------------------------------------
# Design note (same convention Phase 6 established): every exception below
# subclasses an ALREADY-REGISTERED base so FastAPI's MRO-walking handler
# resolution gives it correct HTTP mapping with no new handler function —
# with exactly ONE deliberate exception, `RateLimitExceeded`, which needs
# a 429 status AND a `Retry-After` header that no existing handler can
# produce. That is the only new handler + main.py registration this phase
# adds.
#
# Note also that CacheError and friends are, in normal operation, NEVER
# raised out of a request: `CacheService` catches every Redis failure,
# logs it, and degrades to Postgres. They exist so cache internals can
# signal precisely *what* went wrong to the layer that decides to swallow
# it (and so tests can assert on the specific failure mode), not so route
# handlers can 500 because a cache was unavailable.
class CacheError(NimbusFSException):
    """Base for every cache-layer failure. Degraded to a cache miss by CacheService."""

    def __init__(self, detail: str = "A cache error occurred."):
        super().__init__(detail)


class CacheConnectionError(CacheError):
    """Redis was unreachable, timed out, or the connection pool was exhausted."""

    def __init__(self, detail: str = "The cache backend is unreachable."):
        super().__init__(detail)


class CacheSerializationError(CacheError):
    """
    A value could not be encoded to, or decoded from, the JSON cache
    envelope — including the "envelope schema version is not the one this
    build understands" case, which is treated as a miss rather than an
    application failure (see app/core/cache/serializer.py).
    """

    def __init__(self, detail: str = "Cache value could not be serialized or deserialized."):
        super().__init__(detail)


class DistributedLockError(NimbusFSException):
    """
    Base for distributed-lock failures that are NOT simply "someone else
    holds it" (that stays `LockAcquisitionException`, Phase 4, mapped to
    409). This covers infrastructure-level lock faults: Redis unreachable
    during acquire/release, or a release attempted without ownership.
    """

    def __init__(self, detail: str = "A distributed lock error occurred."):
        super().__init__(detail)


class LockAcquisitionTimeout(LockAcquisitionException):
    """
    Raised when a bounded, retrying acquire (`DistributedLockService.
    acquire`, with `timeout_seconds`) gave up. Subclasses Phase 4's
    `LockAcquisitionException` on purpose: to a client, "I waited and
    still could not get the lock" and "I could not get the lock" are the
    same 409, and the existing handler already says so correctly.
    """

    def __init__(self, detail: str = "Timed out waiting to acquire the required distributed lock."):
        super().__init__(detail)


class LockOwnershipError(DistributedLockError):
    """Raised when a caller tries to release/extend a lock it does not (or no longer) holds."""

    def __init__(self, detail: str = "This lock is not held by the caller."):
        super().__init__(detail)


class RateLimitExceeded(NimbusFSException):
    """
    Raised by `RateLimiter` when a caller's token bucket is empty.

    Carries `retry_after_seconds` (and the budget/remaining figures) so
    the dedicated handler can emit RFC-compliant `Retry-After` and
    `X-RateLimit-*` headers — the one Phase 7 exception that genuinely
    needs its own handler rather than reusing an existing one.
    """

    def __init__(
        self,
        detail: str = "Rate limit exceeded. Please retry later.",
        *,
        retry_after_seconds: int = 1,
        limit: int | None = None,
        remaining: int = 0,
        category: str | None = None,
    ):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.limit = limit
        self.remaining = remaining
        self.category = category
        super().__init__(detail)


# Backwards-friendly alias: the spec names this exception both
# `RateLimitExceeded` and `RateLimitExceededException` in different
# places; both names refer to the same class so neither import breaks.
RateLimitExceededException = RateLimitExceeded

# ---------------------------------------------------------------------
# Event-driven architecture (Phase 8)
# ---------------------------------------------------------------------
# These are WORKER-side exceptions: they are raised inside background
# consumers, never inside an HTTP request, so — unlike every Phase 6/7
# exception — they deliberately need no HTTP status mapping and no
# handler registration. They still subclass `NimbusFSException` so that a
# blanket `except NimbusFSException` anywhere in shared service code
# keeps behaving predictably, and so they are recognizably ours in a log.
#
# The retryable/non-retryable split is the single most important
# classification a consumer makes, because it decides ACK vs NACK:
#   * NACK  -> Pub/Sub redelivers with backoff, and after
#              MAX_DELIVERY_ATTEMPTS routes the message to the DLQ.
#   * ACK   -> the message is gone forever.
# Getting it backwards is expensive in both directions: NACKing a
# permanently-broken message burns the DLQ budget and crash-loops the
# worker on every redelivery, while ACKing a transient failure silently
# drops real work.
class EventProcessingError(NimbusFSException):
    """Base for anything that goes wrong while consuming a domain event."""

    def __init__(self, detail: str = "Event processing failed."):
        super().__init__(detail)


class RetryableEventError(EventProcessingError):
    """
    A transient failure — GCS timeout, database blip, dependency 503.

    The consumer NACKs, and Pub/Sub redelivers per the subscription's
    backoff policy. This is also the DEFAULT classification: any
    unexpected exception escaping `process()` is treated as retryable,
    because "try again" is the safe failure mode when we do not know what
    went wrong.
    """

    def __init__(self, detail: str = "A transient error occurred while processing this event."):
        super().__init__(detail)


class NonRetryableEventError(EventProcessingError):
    """
    A permanent failure — a malformed envelope, an unsupported content
    type, a referenced entity that no longer exists.

    The consumer records `ProcessedEvent(status=FAILED, error=...)` and
    **ACKs**. It deliberately does NOT route to the dead-letter topic:
    the DLQ exists for messages whose *retryable* attempts were
    exhausted and which a human may want to replay after a fix. A file
    whose MIME type this build will never support does not become
    supported by being redelivered five more times — sending it to the
    DLQ would just move noise from one queue to another while hiding the
    real reason in a delivery-attempt counter. The `ProcessedEvent` row
    is the durable, queryable record instead.
    """

    def __init__(self, detail: str = "This event can never be processed successfully."):
        super().__init__(detail)


class EventPublishError(NimbusFSException):
    """
    Raised when handing a message to Pub/Sub fails.

    On the outbox-publisher path this is caught per row and converted
    into `mark_failed` + backoff, so one poisoned row never blocks its
    siblings; the row stays in the outbox and is retried, which is
    exactly the durability the outbox pattern exists to provide.
    """

    def __init__(self, detail: str = "Failed to publish an event to Pub/Sub."):
        super().__init__(detail)
