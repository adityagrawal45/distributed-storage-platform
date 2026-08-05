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

from collections.abc import Awaitable, Callable, AsyncGenerator
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.retry import RetryExhaustedError, retry_async

T = TypeVar("T")

settings = get_settings()


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DATABASE_ECHO,
    # Connection pool sizing (Phase 4): with N replicas each holding up to
    # `pool_size + max_overflow` connections, total connections against
    # Postgres is N * (pool_size + max_overflow) — this must stay under
    # Postgres's `max_connections` as replica count grows. At the current
    # defaults (10 + 20 = 30/replica) that caps horizontal scaling around
    # ~6 replicas against a stock `max_connections=200` Postgres; scaling
    # further requires either raising `max_connections`, lowering
    # per-replica pool size, or introducing PgBouncer in front of
    # Postgres (a future-phase concern, noted here so the ceiling is
    # documented rather than discovered in an incident).
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    # Reclaims stale connections a load balancer/firewall silently
    # dropped instead of surfacing a confusing error on next use.
    pool_pre_ping=True,
    # Recycle connections periodically so long-lived pooled connections
    # don't outlive e.g. a Cloud SQL proxy's own idle timeout.
    pool_recycle=1800,
    # Bounded wait for a free connection from the pool rather than
    # blocking a request indefinitely when the pool is exhausted —
    # surfaces as a clear timeout error instead of a silent hang.
    pool_timeout=30,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency yielding a request-scoped async DB session.

    Implements a Unit-of-Work pattern at the request boundary: if the
    request completes without raising, the transaction is committed;
    otherwise it's rolled back. Repositories only `flush()` (to get
    generated PKs/defaults back) — they never commit — so the entire
    request's writes succeed or fail atomically.
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


async def check_database_connection(*, with_retry: bool = True) -> bool:
    """
    Used by health/readiness endpoints and startup verification.

    `with_retry=False` skips the backoff loop for the fast `/live`
    liveness probe — see `app/database/redis.py::check_redis_connection`
    for the same rationale, mirrored here.
    """
    from sqlalchemy import text

    async def _select_one() -> bool:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    try:
        if not with_retry:
            return await _select_one()
        try:
            return await retry_async(
                _select_one,
                max_attempts=settings.RETRY_MAX_ATTEMPTS,
                base_delay=settings.RETRY_BASE_DELAY_SECONDS,
                max_delay=settings.RETRY_MAX_DELAY_SECONDS,
                operation_name="database_select_1",
            )
        except RetryExhaustedError:
            return False
    except Exception:
        return False


async def close_db_engine() -> None:
    """Called from the application lifespan shutdown hook — disposes the pool."""
    await engine.dispose()


# ---------------------------------------------------------------------
# Read/Write separation (design, not yet wired up)
# ---------------------------------------------------------------------
# NimbusFS currently sends every query — read and write — through the
# single `engine` above, bound to the primary Postgres instance. That is
# the right choice at current scale: read replicas add operational
# complexity (replication lag, routing logic, "read-your-own-write"
# consistency bugs) that isn't worth paying for until the primary is
# actually read-saturated.
#
# The forward path, when it is needed, is a second async engine bound to
# a Cloud SQL read replica's connection string (e.g. via a
# `DATABASE_READ_REPLICA_URL` setting) and a second `get_db_read`
# dependency mirroring `get_db` but read-only (`session.execute` only,
# no `commit`). Read-heavy, staleness-tolerant endpoints (search,
# listing, folder trees) would depend on `get_db_read`; anything that
# writes, or that must read its own just-written data in the same
# request, keeps using `get_db` against the primary. This is a routing
# decision made per-endpoint at the dependency-injection layer, so no
# repository/service code changes — only which session it's handed.


# ---------------------------------------------------------------------
# Transaction management, optimistic locking & deadlock handling
# ---------------------------------------------------------------------
# - Transaction boundary: one transaction per HTTP request (see `get_db`
#   above) — the request either fully succeeds or fully rolls back. This
#   is the simplest correct model and matches the "stateless request"
#   goal of this phase: no transaction survives past a single request on
#   a single replica.
# - Optimistic locking: SQLAlchemy supports version-counter-based
#   optimistic locking natively via `__mapper_args__ = {"version_id_col":
#   ...}` — a column that's incremented on every UPDATE, with the UPDATE
#   statement's WHERE clause pinned to the version it read, so a
#   concurrent writer's UPDATE silently modifying zero rows raises
#   `StaleDataError` instead of one write clobbering another. NimbusFS's
#   existing `FileMetadata.version` column already means something else
#   (the file's *content* version, bumped by `/files/{id}/replace`), so
#   retrofitting a *row* version onto it would conflate two unrelated
#   concepts. The correct future step is a separate `row_version` column
#   (via a new Alembic migration) applied to models with real concurrent-
#   write risk — deferred here rather than shipped half-considered.
# - Deadlock handling: Postgres detects deadlocks itself and aborts one
#   of the two transactions with a `40P01` error, surfaced by SQLAlchemy
#   as `OperationalError`. `run_with_deadlock_retry` below is the
#   reusable primitive for the (rare, but real, once multiple replicas
#   write concurrently) call sites that need "retry the whole unit of
#   work" semantics rather than surfacing the error to the client —
#   deliberately opt-in per call site, not global, since blindly retrying
#   every `OperationalError` would also retry non-transient failures.
DEADLOCK_SQLSTATE = "40P01"


def _is_deadlock(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and DEADLOCK_SQLSTATE in str(getattr(exc, "orig", exc))


async def run_with_deadlock_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
) -> T:
    """
    Runs `operation()` (typically a full "open session, do work, commit"
    unit of work) and retries it if — and only if — it fails with a
    Postgres deadlock. Any other exception propagates immediately.
    """
    settings = get_settings()
    attempts = max_attempts or settings.RETRY_MAX_ATTEMPTS

    async def _guarded() -> T:
        try:
            return await operation()
        except OperationalError as exc:
            if _is_deadlock(exc):
                raise
            raise RetryExhaustedError(1, exc) from exc  # non-deadlock: don't retry, fail immediately

    try:
        return await retry_async(
            _guarded,
            max_attempts=attempts,
            base_delay=settings.RETRY_BASE_DELAY_SECONDS,
            max_delay=settings.RETRY_MAX_DELAY_SECONDS,
            retry_on=(OperationalError,),
            operation_name="db_transaction_deadlock_retry",
        )
    except RetryExhaustedError as exc:
        raise exc.last_exception from exc

