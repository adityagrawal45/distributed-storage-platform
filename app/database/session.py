"""
SQLAlchemy 2.0 async engine and session factory.

Design decisions:
- We use the `asyncpg` driver end-to-end (FastAPI is async, so the whole
  request lifecycle — route -> service -> repository -> DB — stays
  non-blocking).
- `pool_pre_ping=True` avoids "stale connection" errors after the DB or
  a load balancer silently drops idle connections (common in cloud
  environments like Cloud SQL).
- `expire_on_commit=False` avoids extra implicit SELECTs after commit
  when objects are accessed again (e.g. returning the just-created user
  in an API response).
- `get_db` is an async generator dependency: FastAPI will call
  `__anext__` to get the session, and resume it after the response to
  run the `finally` block, guaranteeing the session is always closed
  even if an exception is raised mid-request.
"""

import time
from collections.abc import AsyncGenerator

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.retry import retry_async
from app.logging.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


# ----------------------------------------------------------------------
# Connection pool sizing (per instance): with N=3 replicas behind the
# load balancer and DATABASE_POOL_SIZE=10 + DATABASE_MAX_OVERFLOW=20,
# worst case is 3 * 30 = 90 connections against Postgres. This must stay
# comfortably under Postgres's `max_connections` (default 100, higher on
# Cloud SQL) — pool sizing is therefore a *fleet-wide* budget, not a
# per-instance one, and must be revisited whenever the replica count
# changes (Phase 5's HPA will need this same math applied to max
# replicas, or a connection pooler like PgBouncer in front of Postgres).
# `pool_pre_ping=True` avoids "stale connection" errors after the DB or
# a load balancer silently drops idle connections (common in cloud
# environments like Cloud SQL). `pool_recycle` proactively retires
# connections before a managed proxy (e.g. Cloud SQL Auth Proxy) would
# otherwise reset them out from under us.
# ----------------------------------------------------------------------
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DATABASE_ECHO,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=1800,
)

# Read-replica engine, for future read-heavy endpoints (search, listing)
# to opt into via `get_db_read`. Infrastructure only in this phase: when
# `DATABASE_READ_REPLICA_URL` is unset (the default), this is literally
# the same engine as `engine` and behaves identically — no endpoint is
# rewired to use it yet, so today's behavior is unchanged.
read_engine: AsyncEngine = (
    create_async_engine(
        settings.DATABASE_URL_READ,
        echo=settings.DATABASE_ECHO,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    if settings.DATABASE_READ_REPLICA_URL
    else engine
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

AsyncSessionLocalRead = (
    async_sessionmaker(bind=read_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    if settings.DATABASE_READ_REPLICA_URL
    else AsyncSessionLocal
)


# Errors worth retrying: connection drops, pool exhaustion timeouts, and
# Postgres serialization/deadlock failures (SQLSTATE 40001/40P01) are all
# transient by nature — the same operation retried a moment later
# routinely succeeds. A `ProgrammingError` (bad SQL) or `IntegrityError`
# (constraint violation) is NOT retried: retrying a bug just delays and
# obscures the failure.
_TRANSIENT_DB_ERRORS = (OperationalError, TimeoutError)


def is_retryable_db_error(exc: BaseException) -> bool:
    """
    True for connection-level failures and Postgres deadlock/serialization
    errors (SQLSTATE 40001 = serialization_failure, 40P01 = deadlock_detected).
    Used by `retry_async` callers and available for services that perform
    multi-statement transactions prone to deadlocking under concurrent
    writers (e.g. two instances updating the same row at once).
    """
    if isinstance(exc, _TRANSIENT_DB_ERRORS):
        return True
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
            getattr(exc, "orig", None), "pgcode", None
        )
        return sqlstate in {"40001", "40P01"}
    return False


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency yielding a request-scoped async DB session.

    Implements a Unit-of-Work pattern at the request boundary: if the
    request completes without raising, the transaction is committed;
    otherwise it's rolled back. Repositories only `flush()` (to get
    generated PKs/defaults back) — they never commit — so the entire
    request's writes succeed or fail atomically.

    A `StaleDataError` (optimistic-lock version mismatch, see
    `app/models/file_metadata.py`) or a deadlock surfaces here exactly
    like any other exception: rollback, re-raise, and let
    `app/exceptions/handlers.py` translate it into the right HTTP status
    (409 for a lock conflict) — the session boundary doesn't need to know
    the difference.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db_read() -> AsyncGenerator[AsyncSession, None]:
    """
    Read-only session bound to the replica engine (falls back to the
    primary when no replica is configured). Not wired into any route in
    this phase — provided so a future read-heavy endpoint can opt in
    without any change to `app/database/session.py`.
    """
    async with AsyncSessionLocalRead() as session:
        try:
            yield session
        finally:
            await session.close()


async def _select_1() -> None:
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_database_connection(*, with_retry: bool = False) -> tuple[bool, float]:
    """
    Used by the health check endpoints to verify DB connectivity.

    Returns `(healthy, latency_ms)`. `with_retry=True` (startup) retries
    transient failures with backoff before giving up; `with_retry=False`
    (request-time `/health`/`/ready`) fails fast so a flaky probe doesn't
    slow down every health check.
    """
    start = time.perf_counter()
    try:
        if with_retry:
            await retry_async(
                _select_1,
                attempts=settings.DEPENDENCY_RETRY_ATTEMPTS,
                base_delay=settings.DEPENDENCY_RETRY_BACKOFF_SECONDS,
                max_delay=settings.DEPENDENCY_RETRY_BACKOFF_MAX_SECONDS,
                retry_on=(OperationalError, DBAPIError, OSError),
                operation_name="database_connect",
            )
        else:
            await _select_1()
        healthy = True
    except Exception as exc:
        logger.warning("database_health_check_failed", error=str(exc))
        healthy = False
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return healthy, latency_ms
