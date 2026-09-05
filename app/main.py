"""
NimbusFS FastAPI application entrypoint.

Wires together: settings, logging, middleware, exception handlers, and
the versioned API router. Business logic never lives here — this file
is purely composition.

Phase 4: this is also where the distributed-backend startup/shutdown
lifecycle lives — see `lifespan()` below. Every process that imports
this module and boots the ASGI app is, from this point on, meant to be
one interchangeable replica among N behind a load balancer (see
CONTEXT.md / README §21 "Distributed Backend").
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.observability_routes import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async
from app.core.server_info import get_server_identity
from app.database.gcs import check_storage_connection, get_storage_client
from app.database.redis import check_redis_connection, close_redis_pool
from app.database.session import check_database_connection, close_db_engine
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
from app.exceptions.handlers import (
    authentication_exception_handler,
    authorization_exception_handler,
    circuit_breaker_open_exception_handler,
    conflict_exception_handler,
    domain_exception_handler,
    file_too_large_exception_handler,
    http_exception_handler,
    idempotency_key_in_progress_exception_handler,
    idempotency_key_replayed_exception_handler,
    lock_acquisition_exception_handler,
    not_found_exception_handler,
    rate_limit_exceeded_exception_handler,
    service_unavailable_exception_handler,
    sqlalchemy_exception_handler,
    storage_exception_handler,
    storage_object_not_found_exception_handler,
    storage_permission_exception_handler,
    storage_timeout_exception_handler,
    unhandled_exception_handler,
    unsupported_file_type_exception_handler,
    validation_exception_handler,
)
from app.logging.logger import configure_logging, get_logger
from app.middleware.metrics import MetricsMiddleware
from app.middleware.proxy_headers import TrustedProxyMiddleware
from app.middleware.rate_limit import RateLimitHeadersMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

settings = get_settings()
configure_logging()
logger = get_logger(__name__)


async def _verify_critical_dependencies() -> None:
    """
    Startup dependency verification (Phase 4).

    Runs DB/Redis/Storage checks with the shared retry policy so a
    dependency that's merely slow to come up (e.g. Postgres still
    finishing crash recovery during a coordinated restart) gets a fair
    chance before we give up — but if it never succeeds,
    `FAIL_FAST_ON_STARTUP` means this raises and the process exits
    non-zero instead of accepting traffic it cannot actually serve.
    Kubernetes/Cloud Run then simply don't route to a replica that never
    reaches "ready" — a crash-looping pod is a far clearer operational
    signal than one silently serving 500s or 503s forever.
    """
    checks = {
        "database": check_database_connection(with_retry=True),
        "redis": check_redis_connection(with_retry=True),
        "storage": check_storage_connection(get_storage_client(), with_retry=True),
    }
    results = {}
    for name, check in checks.items():
        results[name] = await check

    failed = [name for name, healthy in results.items() if not healthy]

    if failed:
        logger.error("startup_dependency_check_failed", failed_dependencies=failed)
        if settings.FAIL_FAST_ON_STARTUP:
            raise RuntimeError(
                f"Critical dependencies unreachable at startup: {', '.join(failed)}. "
                "Refusing to accept traffic (FAIL_FAST_ON_STARTUP=true)."
            )
        logger.warning(
            "startup_continuing_despite_failed_dependencies",
            failed_dependencies=failed,
            reason="FAIL_FAST_ON_STARTUP=false",
        )
    else:
        logger.info("startup_dependency_check_passed", dependencies=list(checks.keys()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup/shutdown hooks — the distributed-backend
    lifecycle contract every replica follows identically:

    Startup: connect DB -> connect Redis -> verify storage bucket ->
    (configuration and middleware are already registered by this point,
    by construction, in `create_application()` below) -> logging is
    already initialized at import time (`configure_logging()` above, so
    even import-time errors are logged structured).

    Shutdown: stop accepting new work (uvicorn already stopped routing
    by the time this resumes) -> close DB pool -> close Redis pool ->
    logs are flushed by virtue of using synchronous stdout writes, so
    nothing extra is needed there. Distributed locks are never
    explicitly "released" here by design — every lock has a bounded TTL
    (see `app/core/distributed_lock.py`), so a replica that dies
    uncleanly (kill -9, OOM) self-heals within that TTL without needing
    a shutdown hook to run at all, which a graceful shutdown handler
    cannot assume anyway.
    """
    identity = get_server_identity()
    logger.info(
        "application_startup",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        build_version=settings.BUILD_VERSION,
        git_commit=settings.GIT_COMMIT,
        environment=settings.ENVIRONMENT.value,
        instance_id=identity.instance_id,
        hostname=identity.hostname,
        process_id=identity.process_id,
    )

    await _verify_critical_dependencies()

    logger.info("application_ready", instance_id=identity.instance_id)

    yield

    logger.info("application_shutdown_started", instance_id=identity.instance_id)

    # Give in-flight requests a moment before pools go away. Uvicorn's
    # own `--timeout-graceful-shutdown` (set to >= this value in
    # deployment config) is what actually stops new connections and
    # waits for in-flight ones to finish before this shutdown hook even
    # runs; this is a defense-in-depth ceiling, not the primary
    # mechanism.
    try:
        await retry_async(
            close_db_engine,
            max_attempts=1,
            base_delay=0,
            max_delay=0,
            operation_name="close_db_engine",
        )
    except RetryExhaustedError as exc:
        logger.error("shutdown_db_close_failed", error=str(exc.last_exception))

    try:
        await retry_async(
            close_redis_pool,
            max_attempts=1,
            base_delay=0,
            max_delay=0,
            operation_name="close_redis_pool",
        )
    except RetryExhaustedError as exc:
        logger.error("shutdown_redis_close_failed", error=str(exc.last_exception))

    logger.info("application_shutdown_complete", instance_id=identity.instance_id)


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
    # Middleware (order matters: Starlette runs the LAST-added middleware
    # FIRST on the way in, and last on the way out — so the middleware
    # that must observe/enrich the request earliest is added last here.
    #
    # Execution order (outermost/first to run -> innermost/closest to
    # the route handler):
    #   TrustedProxyMiddleware   -> resolves real client IP/scheme from
    #                               X-Forwarded-* before anything logs
    #                               or reasons about the request
    #   RequestContextMiddleware -> generates request/correlation/trace
    #                               IDs, binds structlog context, logs
    #                               start/completion (needs the above
    #                               already resolved)
    #   RateLimitHeadersMiddleware -> reflects the per-route rate-limit
    #                               decision (made by the `rate_limit(...)`
    #                               dependency) as X-RateLimit-* headers
    #   SecurityHeadersMiddleware
    #   TrustedHostMiddleware
    #   CORSMiddleware
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
    application.add_middleware(RateLimitHeadersMiddleware)
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(TrustedProxyMiddleware)
    # Outermost: times/counts the FULL request including every other
    # middleware's overhead, and must never itself be skipped by an
    # earlier middleware short-circuiting the chain.
    application.add_middleware(MetricsMiddleware)

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
    application.add_exception_handler(IdempotencyKeyInProgressException, idempotency_key_in_progress_exception_handler)
    application.add_exception_handler(IdempotencyKeyReplayedException, idempotency_key_replayed_exception_handler)
    application.add_exception_handler(ConflictException, conflict_exception_handler)
    application.add_exception_handler(FileTooLargeException, file_too_large_exception_handler)
    application.add_exception_handler(UnsupportedFileTypeException, unsupported_file_type_exception_handler)
    application.add_exception_handler(StorageObjectNotFoundException, storage_object_not_found_exception_handler)
    application.add_exception_handler(StoragePermissionException, storage_permission_exception_handler)
    application.add_exception_handler(StorageTimeoutException, storage_timeout_exception_handler)
    application.add_exception_handler(StorageException, storage_exception_handler)
    application.add_exception_handler(LockAcquisitionException, lock_acquisition_exception_handler)
    application.add_exception_handler(CircuitBreakerOpenException, circuit_breaker_open_exception_handler)
    application.add_exception_handler(ServiceUnavailableException, service_unavailable_exception_handler)
    application.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_exception_handler)
    application.add_exception_handler(NimbusFSException, domain_exception_handler)
    application.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    application.include_router(api_router, prefix=settings.API_V1_PREFIX)
    # Unversioned, root-level — see app/api/observability_routes.py's
    # module docstring for why this is not under API_V1_PREFIX.
    application.include_router(metrics_router)

    return application


app = create_application()
