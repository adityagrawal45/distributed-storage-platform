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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
