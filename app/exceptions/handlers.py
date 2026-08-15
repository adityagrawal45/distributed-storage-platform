"""
Global exception handlers.

Every handler here converts an exception into the standardized API
response envelope (see app.schemas.response) so that clients never have
to deal with inconsistent error shapes, regardless of whether the error
came from validation, our domain layer, SQLAlchemy, or an unhandled bug.
"""

import uuid

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.exceptions.custom_exceptions import (
    AuthenticationException,
    AuthorizationException,
    CircuitBreakerOpenException,
    ConflictException,
    FileTooLargeException,
    IdempotencyKeyInProgressException,
    IdempotencyKeyReplayedException,
    LockAcquisitionException,
    NimbusFSException,
    NotFoundException,
    RateLimitExceeded,
    ServiceUnavailableException,
    StorageException,
    StorageObjectNotFoundException,
    StoragePermissionException,
    StorageTimeoutException,
    UnsupportedFileTypeException,
)
from app.logging.logger import get_logger
from app.schemas.response import APIResponse

logger = get_logger(__name__)


def _envelope(
    request: Request, status_code: int, message: str, errors=None, headers: dict[str, str] | None = None
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    body = APIResponse(
        success=False,
        message=message,
        data=None,
        errors=errors,
        request_id=request_id,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers=headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("validation_error", errors=exc.errors(), path=str(request.url))
    errors = [
        {"field": ".".join(str(loc) for loc in err["loc"] if loc != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    return _envelope(request, status.HTTP_422_UNPROCESSABLE_ENTITY, "Validation failed.", errors)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    logger.warning("http_exception", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return _envelope(request, exc.status_code, str(exc.detail))


async def authentication_exception_handler(request: Request, exc: AuthenticationException) -> JSONResponse:
    logger.warning("authentication_failed", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_401_UNAUTHORIZED, exc.detail)


async def authorization_exception_handler(request: Request, exc: AuthorizationException) -> JSONResponse:
    logger.warning("authorization_failed", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_403_FORBIDDEN, exc.detail)


async def not_found_exception_handler(request: Request, exc: NotFoundException) -> JSONResponse:
    logger.info("not_found", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_404_NOT_FOUND, exc.detail)


async def conflict_exception_handler(request: Request, exc: ConflictException) -> JSONResponse:
    logger.info("conflict", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_409_CONFLICT, exc.detail)


async def domain_exception_handler(request: Request, exc: NimbusFSException) -> JSONResponse:
    logger.error("domain_exception", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_400_BAD_REQUEST, exc.detail)


async def file_too_large_exception_handler(request: Request, exc: FileTooLargeException) -> JSONResponse:
    logger.info("file_too_large", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, exc.detail)


async def unsupported_file_type_exception_handler(request: Request, exc: UnsupportedFileTypeException) -> JSONResponse:
    logger.info("unsupported_file_type", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, exc.detail)


async def storage_object_not_found_exception_handler(
    request: Request, exc: StorageObjectNotFoundException
) -> JSONResponse:
    logger.info("storage_object_not_found", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_404_NOT_FOUND, exc.detail)


async def storage_permission_exception_handler(request: Request, exc: StoragePermissionException) -> JSONResponse:
    logger.error("storage_permission_denied", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_403_FORBIDDEN, exc.detail)


async def storage_timeout_exception_handler(request: Request, exc: StorageTimeoutException) -> JSONResponse:
    logger.error("storage_timeout", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_504_GATEWAY_TIMEOUT, exc.detail)


async def storage_exception_handler(request: Request, exc: StorageException) -> JSONResponse:
    """Catch-all for storage failures not covered by a more specific handler above."""
    logger.error("storage_error", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_502_BAD_GATEWAY, exc.detail)


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error("database_error", error=str(exc), path=str(request.url))
    return _envelope(request, status.HTTP_503_SERVICE_UNAVAILABLE, "A database error occurred.")


async def lock_acquisition_exception_handler(request: Request, exc: LockAcquisitionException) -> JSONResponse:
    logger.warning("lock_acquisition_failed", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_409_CONFLICT, exc.detail)


async def circuit_breaker_open_exception_handler(request: Request, exc: CircuitBreakerOpenException) -> JSONResponse:
    logger.error("circuit_breaker_open", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_503_SERVICE_UNAVAILABLE, exc.detail)


async def service_unavailable_exception_handler(request: Request, exc: ServiceUnavailableException) -> JSONResponse:
    logger.error("service_unavailable", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_503_SERVICE_UNAVAILABLE, exc.detail)


async def idempotency_key_replayed_exception_handler(
    request: Request, exc: IdempotencyKeyReplayedException
) -> JSONResponse:
    logger.warning("idempotency_key_replayed_with_different_body", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail)


async def idempotency_key_in_progress_exception_handler(
    request: Request, exc: IdempotencyKeyInProgressException
) -> JSONResponse:
    logger.info("idempotency_key_in_progress", detail=exc.detail, path=str(request.url))
    return _envelope(request, status.HTTP_409_CONFLICT, exc.detail)


async def rate_limit_exceeded_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    The ONE new exception handler Phase 7 required (see the design note in
    `custom_exceptions.py`): every other new exception subclasses an
    already-registered base and is mapped correctly for free.

    It exists because a 429 is not just a status code — a well-behaved API
    tells the client exactly when to come back. `Retry-After` is computed
    by the token bucket from the real token deficit, not guessed, so a
    client that honors it will succeed on its first retry instead of
    hammering and re-triggering the limit.
    """
    logger.warning(
        "rate_limit_rejected_response",
        category=exc.category,
        limit=exc.limit,
        remaining=exc.remaining,
        retry_after_seconds=exc.retry_after_seconds,
        path=str(request.url),
    )
    headers = {
        "Retry-After": str(exc.retry_after_seconds),
        "X-RateLimit-Remaining": str(exc.remaining),
    }
    if exc.limit is not None:
        headers["X-RateLimit-Limit"] = str(exc.limit)
    if exc.category:
        headers["X-RateLimit-Category"] = exc.category
    return _envelope(request, status.HTTP_429_TOO_MANY_REQUESTS, exc.detail, headers=headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc), path=str(request.url))
    return _envelope(request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")
