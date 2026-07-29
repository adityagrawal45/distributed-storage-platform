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

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DATABASE_ECHO,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
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


async def check_database_connection() -> bool:
    """Used by the health check endpoint to verify DB connectivity."""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
