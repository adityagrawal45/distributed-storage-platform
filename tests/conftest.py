"""
Shared pytest fixtures.

Design decisions:
- Tests run against an in-memory SQLite database (via `aiosqlite`),
  NOT the real PostgreSQL used in dev/prod. This keeps the test suite
  fast and hermetic (no external services required to run `pytest`).
  Trade-off: Postgres-specific features (native ENUM type, etc.) are
  approximated by SQLAlchemy's cross-dialect abstractions, which is
  sufficient for Phase 1's models. Integration tests against real
  Postgres can be added in a later phase via `docker-compose`.
- `app.dependency_overrides` swaps `get_db` for a fixture-scoped async
  session, so route handlers under test hit SQLite transparently
  without any application code knowing the difference.
- Each test function gets a fresh schema (`create_all` before,
  `drop_all` after) for full isolation between tests.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database.redis as redis_db
from app.database.session import Base, get_db
from app.dependencies.providers import get_gcs_client
from app.main import app
from tests.fakes.fake_gcs import FakeGCSClient

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestSessionLocal = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def fake_gcs_client() -> FakeGCSClient:
    """A fresh in-memory fake GCS client per test — see tests/fakes/fake_gcs.py."""
    return FakeGCSClient()


@pytest.fixture
def fake_redis_client(monkeypatch) -> fake_aioredis.FakeRedis:
    """
    A fresh in-memory fake Redis per test (Phase 4) — mirrors
    `fake_gcs_client`'s role for GCS: the test suite stays hermetic, no
    real Redis process required.

    Monkeypatches `app.database.redis.get_redis_client` (the ONE seam
    every Phase 4 Redis consumer — cache service, distributed lock,
    idempotency/rate-limit middleware, health checks — is required to
    call through, by convention; see `app/database/redis.py`'s
    docstring) so a single patch point covers all of them at once. A
    single `FakeRedis` instance is reused for every `get_redis_client()`
    call within the test (each caller still calls its own `.aclose()`,
    which fakeredis tolerates without losing its in-memory state).
    """
    fake_client = fake_aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_db, "get_redis_client", lambda: fake_client)
    return fake_client


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, fake_gcs_client: FakeGCSClient, fake_redis_client: fake_aioredis.FakeRedis
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gcs_client] = lambda: fake_gcs_client

    # Simulates a fully-started instance: real deployments only reach
    # this state once `app.main`'s `lifespan` startup checks pass, but
    # `httpx.ASGITransport` never drives the ASGI lifespan protocol, so
    # `app.state.ready` would otherwise stay at its unstarted default
    # (False) for every test. Tests that specifically exercise
    # not-ready/draining behavior override this explicitly.
    app.state.ready = True
    app.state.active_requests = 0

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def valid_user_payload() -> dict:
    return {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@nimbusfs.io",
        "password": "StrongP@ssw0rd",
    }


@pytest_asyncio.fixture
async def authed_client(client: AsyncClient, valid_user_payload: dict) -> AsyncClient:
    """
    A client pre-registered, pre-logged-in, and carrying a valid
    `Authorization: Bearer` header — used by Phase 2 tests (folders/
    metadata) that need an authenticated owner but aren't testing auth
    itself.
    """
    await client.post("/api/v1/auth/register", json=valid_user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={"username": valid_user_payload["email"], "password": valid_user_payload["password"]},
    )
    access_token = login_response.json()["data"]["access_token"]
    client.headers["Authorization"] = f"Bearer {access_token}"
    return client