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