"""
NimbusFS FastAPI application entrypoint.

Wires together: settings, logging, middleware, exception handlers, and
the versioned API router. Business logic never lives here — this file
is purely composition.

Phase 4 turns this into a *distributed* backend entrypoint: startup now
fails fast if a critical dependency (DB/Redis/GCS) is unreachable rather
than silently starting a broken instance, and shutdown drains in-flight
requests before tearing down connection pools. See the `lifespan`
function below for the full startup/shutdown lifecycle, and README
"Startup Lifecycle" / "Shutdown Lifecycle" for the narrative version.
"""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.circuit_breaker import CircuitOpenError
from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async
from app.core.server_identity import SERVER_IDENTITY
from app.database.gcs import check_storage_connection, get_storage_client
from app.database.redis import check_redis_connection, redis_pool
from app.database.session import check_database_connection, engine
from app.exceptions.custom_exceptions import (
    AuthenticationException,
    AuthorizationException,
    ConflictException,
    FileTooLargeException,
    LockAcquisitionException,
    NimbusFSException,
    NotFoundException,
    StorageException,
    StorageObjectNotFoundException,
    StoragePermissionException,
    StorageTimeoutException,
    UnsupportedFileTypeException,
)
from app.exceptions.handlers import (
    authentication_exception_handler,
    authorization_exception_handler,
    circuit_open_exception_handler,
    conflict_exception_handler,
    domain_exception_handler,
    file_too_large_exception_handler,
    http_exception_handler,
    lock_acquisition_exception_handler,
    not_found_exception_handler,
    retry_exhausted_exception_handler,
    sqlalchemy_exception_handler,
    stale_data_exception_handler,
    storage_exception_handler,
    storage_object_not_found_exception_handler,
    storage_permission_exception_handler,
    storage_timeout_exception_handler,
    unhandled_exception_handler,
    unsupported_file_type_exception_handler,
    validation_exception_handler,
)
from app.logging.logger import configure_logging, get_logger
from app.middleware.idempotency import IdempotencyMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

settings = get_settings()
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup/shutdown hooks.

    Startup order (each step's failure aborts startup — see README
    "Startup Lifecycle"):
      1. Load configuration (already done at import time, above).
      2. Register middleware/exception handlers (done in
         `create_application`, before `lifespan` ever runs).
      3. Initialize logging (already done at import time, above).
      4. Connect to PostgreSQL — retried with backoff, then fail fast.
      5. Connect to Redis — retried with backoff, then fail fast.
      6. Verify the GCS bucket is reachable — retried with backoff, then
         fail fast.
      7. Flip `app.state.ready = True`. Only now does `/ready` start
         returning 200 — a freshly started instance behind a load
         balancer receives zero traffic until this line runs (graceful
         startup).

    Shutdown order (see README "Shutdown Lifecycle"):
      1. Flip `app.state.ready = False` FIRST, before anything else, so
         `/ready` starts failing immediately and the load balancer stops
         routing new requests to this instance (this is the entire point
         of doing it first — draining only works if new traffic stops
         arriving during the drain).
      2. Wait for in-flight requests to finish (poll
         `app.state.active_requests`, tracked by
         `RequestContextMiddleware`, up to `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`).
      3. Close the database connection pool.
      4. Close the Redis connection pool.
      5. Log shutdown completion (flushing stdout is implicit — structlog
         writes synchronously to `sys.stdout` on every call, there is no
         separate buffer to flush).

    Note: releasing distributed locks explicitly is NOT needed here —
    every `DistributedLock` carries a mandatory TTL (see
    `app/core/distributed_lock.py`) specifically so a killed process
    can never leave a lock held forever; they self-heal via expiry.
    """
    app.state.ready = False
    app.state.active_requests = 0
    startup_start = time.perf_counter()

    logger.info(
        "application_startup_begin",
        app_name=settings.APP_NAME,
        **SERVER_IDENTITY.as_dict(),
    )

    async def _require_database() -> None:
        healthy, _ = await check_database_connection(with_retry=True)
        if not healthy:
            raise RuntimeError("Database is unreachable after retries.")

    async def _require_redis() -> None:
        healthy, _ = await check_redis_connection(with_retry=True)
        if not healthy:
            raise RuntimeError("Redis is unreachable after retries.")

    async def _require_storage() -> None:
        healthy, _ = await check_storage_connection(get_storage_client(), with_retry=True)
        if not healthy:
            raise RuntimeError(f"GCS bucket '{settings.GCS_BUCKET_NAME}' is unreachable after retries.")

    try:
        await _require_database()
        logger.info("startup_check_passed", dependency="database")
        await _require_redis()
        logger.info("startup_check_passed", dependency="redis")
        await _require_storage()
        logger.info("startup_check_passed", dependency="storage")
    except Exception:
        logger.exception("application_startup_failed")
        raise

    app.state.ready = True
    logger.info(
        "application_startup_complete",
        duration_ms=round((time.perf_counter() - startup_start) * 1000, 2),
        instance_id=SERVER_IDENTITY.instance_id,
    )

    yield

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    shutdown_start = time.perf_counter()
    app.state.ready = False
    logger.info("application_shutdown_begin", instance_id=SERVER_IDENTITY.instance_id)

    deadline = time.perf_counter() + settings.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
    while getattr(app.state, "active_requests", 0) > 0 and time.perf_counter() < deadline:
        await asyncio.sleep(0.1)

    remaining = getattr(app.state, "active_requests", 0)
    if remaining:
        logger.warning("shutdown_drain_timed_out", remaining_requests=remaining)

    await engine.dispose()
    await redis_pool.disconnect()

    logger.info(
        "application_shutdown_complete",
        duration_ms=round((time.perf_counter() - shutdown_start) * 1000, 2),
    )


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
    # way in, last on the way out — see each middleware's own docstring
    # for why it sits where it does).
    #
    # Execution order, outermost to innermost:
    #   RequestContextMiddleware  - always runs first/last; establishes
    #                               request_id/correlation_id/trace_id
    #                               and measures TRUE end-to-end duration,
    #                               including every middleware below.
    #   SecurityHeadersMiddleware - hardening headers on EVERY response,
    #                               including short-circuits from the
    #                               middlewares below it (rate limit 429s,
    #                               idempotent replays).
    #   TrustedHostMiddleware     - reject a forged Host header before
    #                               spending any work (rate-limit lookup,
    #                               idempotency lookup) on the request.
    #   RateLimitMiddleware       - cheap Redis INCR; reject abusive
    #                               traffic before the idempotency store
    #                               does its (slightly heavier) lookup.
    #   IdempotencyMiddleware     - replay a cached response or claim an
    #                               in-progress marker before the request
    #                               reaches routing/auth/business logic.
    #   CORSMiddleware            - innermost by original design (see
    #                               git history): wraps only the actual
    #                               route dispatch, not our own
    #                               cross-cutting concerns above.
    # ------------------------------------------------------------------
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(IdempotencyMiddleware)
    application.add_middleware(RateLimitMiddleware)
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
    application.add_exception_handler(LockAcquisitionException, lock_acquisition_exception_handler)
    application.add_exception_handler(ConflictException, conflict_exception_handler)
    application.add_exception_handler(FileTooLargeException, file_too_large_exception_handler)
    application.add_exception_handler(UnsupportedFileTypeException, unsupported_file_type_exception_handler)
    application.add_exception_handler(StorageObjectNotFoundException, storage_object_not_found_exception_handler)
    application.add_exception_handler(StoragePermissionException, storage_permission_exception_handler)
    application.add_exception_handler(StorageTimeoutException, storage_timeout_exception_handler)
    application.add_exception_handler(StorageException, storage_exception_handler)
    application.add_exception_handler(NimbusFSException, domain_exception_handler)
    application.add_exception_handler(StaleDataError, stale_data_exception_handler)
    application.add_exception_handler(CircuitOpenError, circuit_open_exception_handler)
    application.add_exception_handler(RetryExhaustedError, retry_exhausted_exception_handler)
    application.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    application.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return application


app = create_application()
