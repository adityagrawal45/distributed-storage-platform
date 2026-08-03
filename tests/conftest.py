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
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, fake_gcs_client: FakeGCSClient) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gcs_client] = lambda: fake_gcs_client

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