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
    ConflictException,
    NimbusFSException,
    NotFoundException,
)
from app.logging.logger import get_logger
from app.schemas.response import APIResponse

logger = get_logger(__name__)


def _envelope(request: Request, status_code: int, message: str, errors=None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    body = APIResponse(
        success=False,
        message=message,
        data=None,
        errors=errors,
        request_id=request_id,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


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


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error("database_error", error=str(exc), path=str(request.url))
    return _envelope(request, status.HTTP_503_SERVICE_UNAVAILABLE, "A database error occurred.")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc), path=str(request.url))
    return _envelope(request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")
