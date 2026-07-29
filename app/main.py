<<<<<<< HEAD
"""
NimbusFS FastAPI application entrypoint.

Wires together: settings, logging, middleware, exception handlers, and
the versioned API router. Business logic never lives here — this file
is purely composition.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.database.redis import redis_pool
from app.database.session import engine
from app.exceptions.custom_exceptions import (
    AuthenticationException,
    AuthorizationException,
    ConflictException,
    NimbusFSException,
    NotFoundException,
)
from app.exceptions.handlers import (
    authentication_exception_handler,
    authorization_exception_handler,
    conflict_exception_handler,
    domain_exception_handler,
    http_exception_handler,
    not_found_exception_handler,
    sqlalchemy_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging.logger import configure_logging, get_logger
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

settings = get_settings()
configure_logging()
logger = get_logger(__name__)
=======
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging
import uuid
import time
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import AppException
from app.api.v1 import auth, health, users
from app.infrastructure.database import engine
from app.infrastructure.redis_client import init_redis_pool
from app.domain.models import Base
from app.utils.response import error_response

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115


@asynccontextmanager
async def lifespan(app: FastAPI):
<<<<<<< HEAD
    """Application startup/shutdown hooks."""
    logger.info(
        "application_startup",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT.value,
    )
    yield
    logger.info("application_shutdown")
    await engine.dispose()
    await redis_pool.disconnect()


def create_application() -> FastAPI:
    application = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Middleware (order matters: outermost added last runs first on the
    # way in, last on the way out. We want security headers and request
    # context to wrap everything, including CORS handling.)
    # ------------------------------------------------------------------
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(RequestContextMiddleware)

    # ------------------------------------------------------------------
    # Exception handlers (order matters: FastAPI matches most specific
    # registered exception type first, so subclasses are registered
    # before their parent classes where it matters).
    # ------------------------------------------------------------------
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(AuthenticationException, authentication_exception_handler)
    application.add_exception_handler(AuthorizationException, authorization_exception_handler)
    application.add_exception_handler(NotFoundException, not_found_exception_handler)
    application.add_exception_handler(ConflictException, conflict_exception_handler)
    application.add_exception_handler(NimbusFSException, domain_exception_handler)
    application.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    application.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return application


app = create_application()
=======
    # Startup
    logger.info("Starting up application...")
    # Initialize Redis pool
    await init_redis_pool()
    # Create tables if not exist (in production use migrations)
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.create_all)
    logger.info("Database and Redis connections established")
    yield
    # Shutdown
    logger.info("Shutting down...")
    await engine.dispose()
    # Redis pool cleanup will happen automatically


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted Host (optional)
# app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# Middleware for request-id and logging
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(process_time)
    logger.info(
        f"Request {request.method} {request.url.path}",
        extra={"request_id": request_id, "status": response.status_code, "duration": process_time}
    )
    return response


# Global exception handlers
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    logger.error(f"AppException: {exc.message}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=exc.message,
            error_code=exc.error_code,
            details=exc.details,
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning(f"HTTPException: {exc.detail}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=exc.detail,
            error_code="http_error",
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    details = {}
    for error in errors:
        field = ".".join(str(loc) for loc in error["loc"])
        details[field] = error["msg"]
    logger.warning(f"Validation error: {details}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_response(
            message="Validation error",
            error_code="validation_error",
            details=details,
        ),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response(
            message="Internal server error",
            error_code="internal_error",
        ),
    )


# Include routers
app.include_router(auth.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")

# Root endpoint
@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}"}
>>>>>>> b62d862acc4e93e3c4a06e1dd0022682031f3115
