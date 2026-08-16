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
- Phase 4: `/health`/`/ready` deliberately check *real* DB/Redis
  connectivity (see app/database/session.py, app/database/redis.py —
  these health checks intentionally bypass the SQLite/fake overrides so
  they report the truth about actual infra reachability). Against an
  unreachable Postgres/Redis in a sandboxed test environment, the
  built-in retry/backoff (`Settings.RETRY_MAX_ATTEMPTS`) would otherwise
  add several real seconds per health-check call across the suite. The
  env vars below are set *before* `app.main` (and therefore
  `get_settings()`) is ever imported, so the test suite trades that
  retry resilience for speed — this changes nothing about
  production/dev behavior, which reads these from the real environment.
- Phase 6: `CHUNK_MIN_SIZE_BYTES` is similarly lowered for tests only —
  the production default (1 MiB) would make every chunked-upload test
  push real megabyte-sized byte buffers through SQLite/FakeGCSClient
  for no benefit; a 1 KiB floor keeps `tests/test_chunked_upload.py`
  fast while still exercising the exact same code paths.
"""

import os

os.environ.setdefault("RETRY_MAX_ATTEMPTS", "1")
os.environ.setdefault("RETRY_BASE_DELAY_SECONDS", "0.01")
os.environ.setdefault("RETRY_MAX_DELAY_SECONDS", "0.05")
os.environ.setdefault("CHUNK_MIN_SIZE_BYTES", "1024")

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.redis import get_redis
from app.database.session import Base, get_db
from app.dependencies.providers import get_event_publisher, get_gcs_client
from app.events.publisher import EventPublisher
from app.main import app
from tests.fakes.fake_gcs import FakeGCSClient
from tests.fakes.fake_pubsub import FakePublisherClient
from tests.fakes.fake_redis import FakeRedisClient

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
def fake_redis_client() -> FakeRedisClient:
    """A fresh in-memory fake Redis client per test — see tests/fakes/fake_redis.py."""
    return FakeRedisClient()


@pytest.fixture
def fake_pubsub_client() -> FakePublisherClient:
    """
    A fresh in-memory fake Pub/Sub publisher per test — see
    tests/fakes/fake_pubsub.py.

    Note the API itself never publishes directly (services write to the
    transactional outbox instead), so this override exists mainly so that
    no test can ever reach real Pub/Sub even by accident, and so worker
    tests can share one construction path with HTTP-level tests.
    """
    return FakePublisherClient()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    fake_gcs_client: FakeGCSClient,
    fake_redis_client: FakeRedisClient,
    fake_pubsub_client: FakePublisherClient,
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        yield db_session

    async def _override_get_redis():
        yield fake_redis_client

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gcs_client] = lambda: fake_gcs_client
    app.dependency_overrides[get_redis] = _override_get_redis
    # Phase 8: `enabled=True` forces the real publish path through the
    # fake, so a test that DOES publish exercises the executor/wrap_future
    # bridge rather than the PUBSUB_ENABLED=False no-op branch.
    app.dependency_overrides[get_event_publisher] = lambda: EventPublisher(
        fake_pubsub_client, enabled=True
    )

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