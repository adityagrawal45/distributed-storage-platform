"""
Phase 7 tests — distributed rate limiting.

Covers: requests within budget, requests over budget, the exact 429
contract (status, envelope, `Retry-After`, `X-RateLimit-*`), refill over
time, concurrency (N replicas / N concurrent requests share ONE bucket),
bucket isolation across identities and categories, manual reset, the
disabled switch, and both Redis-failure policies (fail-open and
fail-closed).

Time is controlled, never slept through: `RateLimiter` passes `now_ms`
into the Lua script rather than reading the clock inside it (see that
module for why), which makes "the bucket refills after the window" a
deterministic assertion instead of a 60-second test.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.core.cache.keys import CacheKeyBuilder
from app.core.config.settings import Settings
from app.core.rate_limiter import RateLimitCategory, RateLimiter, RateLimitRule
from app.dependencies.rate_limit import get_rate_limiter
from app.main import app
from tests.fakes.fake_redis import FakeRedisClient


class ControllableTime:
    """Stand-in for the `time` module inside `app.core.rate_limiter`."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._now = start

    def time(self) -> float:
        return self._now

    def perf_counter(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def clock(monkeypatch) -> ControllableTime:
    fake = ControllableTime()
    monkeypatch.setattr("app.core.rate_limiter.time", fake)
    return fake


def build_limiter(client: FakeRedisClient, **overrides) -> RateLimiter:
    settings = Settings(**overrides)
    return RateLimiter(client, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX))


IDENTITY = "user:00000000-0000-0000-0000-000000000001"


# =====================================================================
# Rule arithmetic
# =====================================================================
class TestRateLimitRule:
    def test_capacity_is_the_burst_and_refill_is_the_sustained_rate(self):
        rule = RateLimitRule(requests=60, window_seconds=60)
        assert rule.capacity == 60.0
        assert rule.refill_rate_per_second == 1.0

    def test_a_short_window_refills_faster(self):
        assert RateLimitRule(10, 5).refill_rate_per_second == 2.0


# =====================================================================
# Core limiter behavior
# =====================================================================
class TestRateLimiterCore:
    async def test_requests_within_budget_are_allowed(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=5, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        for _ in range(5):
            result = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
            assert result.allowed is True
        assert result.remaining == 0

    async def test_the_request_past_the_budget_is_rejected(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=3, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        for _ in range(3):
            assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is True

        rejected = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert rejected.allowed is False
        assert rejected.remaining == 0
        assert rejected.retry_after_seconds >= 1

    async def test_retry_after_is_computed_from_the_real_deficit(self, fake_redis_client, clock):
        """10 requests / 60s => 1 token every 6s, so a 1-token deficit is ~6s."""
        limiter = build_limiter(
            fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=10, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60
        )
        for _ in range(10):
            await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        rejected = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert 5 <= rejected.retry_after_seconds <= 7

    async def test_the_bucket_refills_as_time_passes(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=6, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        for _ in range(6):
            await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is False

        clock.advance(10)  # 6/60 tokens per second => 1 token back
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is True
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is False

    async def test_a_fully_idle_window_restores_the_whole_budget(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=4, RATE_LIMIT_LOGIN_WINDOW_SECONDS=30)
        for _ in range(4):
            await limiter.check(RateLimitCategory.LOGIN, IDENTITY)

        clock.advance(30)
        for _ in range(4):
            assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is True

    async def test_refill_never_exceeds_capacity(self, fake_redis_client, clock):
        """A long idle period must not let a client bank unlimited burst."""
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=3, RATE_LIMIT_LOGIN_WINDOW_SECONDS=10)
        await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        clock.advance(100_000)

        allowed = 0
        for _ in range(20):
            if (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed:
                allowed += 1
        assert allowed == 3

    async def test_concurrent_requests_share_one_bucket(self, fake_redis_client, clock):
        """
        The property an in-process limiter cannot provide: 20 simultaneous
        requests (conceptually across N replicas) draw from ONE budget.
        """
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_METADATA_REQUESTS=5, RATE_LIMIT_METADATA_WINDOW_SECONDS=60)
        results = await asyncio.gather(
            *(limiter.check(RateLimitCategory.METADATA, IDENTITY) for _ in range(20))
        )
        assert sum(1 for r in results if r.allowed) == 5

    async def test_identities_have_independent_buckets(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=2, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        for _ in range(2):
            await limiter.check(RateLimitCategory.LOGIN, "user:alice")
        assert (await limiter.check(RateLimitCategory.LOGIN, "user:alice")).allowed is False
        assert (await limiter.check(RateLimitCategory.LOGIN, "user:bob")).allowed is True

    async def test_categories_have_independent_buckets(self, fake_redis_client, clock):
        """Exhausting search must not stop a user finishing an upload."""
        limiter = build_limiter(
            fake_redis_client,
            RATE_LIMIT_SEARCH_REQUESTS=2,
            RATE_LIMIT_SEARCH_WINDOW_SECONDS=60,
            RATE_LIMIT_UPLOAD_COMPLETE_REQUESTS=5,
            RATE_LIMIT_UPLOAD_COMPLETE_WINDOW_SECONDS=60,
        )
        for _ in range(2):
            await limiter.check(RateLimitCategory.SEARCH, IDENTITY)
        assert (await limiter.check(RateLimitCategory.SEARCH, IDENTITY)).allowed is False
        assert (await limiter.check(RateLimitCategory.UPLOAD_COMPLETE, IDENTITY)).allowed is True

    async def test_reset_restores_the_full_budget(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=1, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is False

        assert await limiter.reset(RateLimitCategory.LOGIN, IDENTITY) is True
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is True

    async def test_peek_reports_budget_without_consuming(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=3, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        peeked = await limiter.peek(RateLimitCategory.LOGIN, IDENTITY)
        assert peeked.remaining == 2
        assert (await limiter.peek(RateLimitCategory.LOGIN, IDENTITY)).remaining == 2

    async def test_disabled_limiter_always_allows(self, fake_redis_client, clock):
        limiter = build_limiter(
            fake_redis_client, RATE_LIMIT_ENABLED=False, RATE_LIMIT_LOGIN_REQUESTS=1
        )
        for _ in range(50):
            assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).allowed is True
        assert fake_redis_client.command_counts == {}

    async def test_unknown_category_falls_back_to_the_default_rule(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_DEFAULT_REQUESTS=7)
        assert limiter.rule_for(RateLimitCategory.DEFAULT).requests == 7


# =====================================================================
# Redis failure policy
# =====================================================================
class TestRateLimiterFailurePolicy:
    async def test_fail_open_allows_when_redis_is_down(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_FAIL_OPEN=True, RATE_LIMIT_LOGIN_REQUESTS=1)
        fake_redis_client.start_failing()

        result = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert result.allowed is True
        assert result.degraded is True, "the degradation must be reported, not hidden"

    async def test_fail_closed_rejects_when_redis_is_down(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_FAIL_OPEN=False, RATE_LIMIT_LOGIN_REQUESTS=100)
        fake_redis_client.start_failing()

        result = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert result.allowed is False
        assert result.degraded is True
        assert result.retry_after_seconds > 0

    async def test_limiter_recovers_once_redis_returns(self, fake_redis_client, clock):
        limiter = build_limiter(fake_redis_client, RATE_LIMIT_LOGIN_REQUESTS=2, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        fake_redis_client.start_failing()
        assert (await limiter.check(RateLimitCategory.LOGIN, IDENTITY)).degraded is True

        fake_redis_client.stop_failing()
        recovered = await limiter.check(RateLimitCategory.LOGIN, IDENTITY)
        assert recovered.degraded is False
        assert recovered.allowed is True


# =====================================================================
# HTTP integration — the 429 contract
# =====================================================================
@pytest.fixture
def tight_limits(fake_redis_client: FakeRedisClient):
    """
    Overrides the limiter provider with tiny budgets so the HTTP contract
    can be exercised in a handful of requests. Uses the same
    `FakeRedisClient` the rest of the request stack has, so bucket state
    is genuinely shared across the calls under test.
    """
    settings = Settings(
        RATE_LIMIT_LOGIN_REQUESTS=2,
        RATE_LIMIT_LOGIN_WINDOW_SECONDS=60,
        RATE_LIMIT_REGISTER_REQUESTS=2,
        RATE_LIMIT_REGISTER_WINDOW_SECONDS=300,
        RATE_LIMIT_METADATA_REQUESTS=1000,
        RATE_LIMIT_METADATA_WINDOW_SECONDS=60,
        RATE_LIMIT_SEARCH_REQUESTS=2,
        RATE_LIMIT_SEARCH_WINDOW_SECONDS=60,
    )
    limiter = RateLimiter(fake_redis_client, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_rate_limiter, None)


class TestRateLimitHTTPContract:
    async def test_login_returns_429_with_a_retry_after_header(
        self, client: AsyncClient, valid_user_payload: dict, tight_limits
    ):
        await client.post("/api/v1/auth/register", json=valid_user_payload)
        credentials = {"username": valid_user_payload["email"], "password": "WrongPassword1!"}

        first = await client.post("/api/v1/auth/login", data=credentials)
        second = await client.post("/api/v1/auth/login", data=credentials)
        assert first.status_code != 429 and second.status_code != 429

        third = await client.post("/api/v1/auth/login", data=credentials)
        assert third.status_code == 429
        assert int(third.headers["Retry-After"]) >= 1
        assert third.headers["X-RateLimit-Limit"] == "2"
        assert third.headers["X-RateLimit-Remaining"] == "0"
        assert third.headers["X-RateLimit-Category"] == "login"

    async def test_429_uses_the_standard_api_response_envelope(
        self, client: AsyncClient, valid_user_payload: dict, tight_limits
    ):
        await client.post("/api/v1/auth/register", json=valid_user_payload)
        credentials = {"username": valid_user_payload["email"], "password": "WrongPassword1!"}
        for _ in range(3):
            response = await client.post("/api/v1/auth/login", data=credentials)

        body = response.json()
        assert response.status_code == 429
        assert body["success"] is False
        assert body["data"] is None
        assert "request_id" in body and "timestamp" in body
        assert "rate limit exceeded" in body["message"].lower()

    async def test_registration_has_its_own_stricter_budget(self, client: AsyncClient, tight_limits):
        base = {"first_name": "A", "last_name": "B", "password": "StrongP@ssw0rd"}
        for index in range(2):
            response = await client.post(
                "/api/v1/auth/register", json={**base, "email": f"user{index}@nimbusfs.io"}
            )
            assert response.status_code != 429

        blocked = await client.post("/api/v1/auth/register", json={**base, "email": "user9@nimbusfs.io"})
        assert blocked.status_code == 429

    async def test_successful_responses_carry_ratelimit_headers(
        self, authed_client: AsyncClient, tight_limits
    ):
        response = await authed_client.get("/api/v1/folders")
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "1000"
        assert int(response.headers["X-RateLimit-Remaining"]) < 1000
        assert response.headers["X-RateLimit-Category"] == "metadata"

    async def test_unlimited_routes_still_report_the_header_shape(self, client: AsyncClient):
        """Phase 4's contract preserved: absent limits are labelled, not omitted."""
        response = await client.get("/api/v1/live")
        assert response.headers["X-RateLimit-Limit"] == "unlimited"

    async def test_search_has_a_tighter_budget_than_the_router_default(
        self, authed_client: AsyncClient, tight_limits
    ):
        for _ in range(2):
            assert (await authed_client.get("/api/v1/metadata/search", params={"q": "x"})).status_code == 200

        blocked = await authed_client.get("/api/v1/metadata/search", params={"q": "x"})
        assert blocked.status_code == 429
        assert blocked.headers["X-RateLimit-Category"] == "search"

        # The shared metadata budget is untouched, so plain reads still work.
        assert (await authed_client.get("/api/v1/folders")).status_code == 200

    async def test_over_limit_requests_never_reach_the_handler(
        self, client: AsyncClient, valid_user_payload: dict, tight_limits
    ):
        """
        A rejected request must cost no database work — the whole reason
        the limiter runs as a dependency rather than inside the handler.
        """
        await client.post("/api/v1/auth/register", json=valid_user_payload)
        good = {"username": valid_user_payload["email"], "password": valid_user_payload["password"]}
        for _ in range(2):
            await client.post("/api/v1/auth/login", data=good)

        blocked = await client.post("/api/v1/auth/login", data=good)
        assert blocked.status_code == 429
        assert blocked.json()["data"] is None, "no token was issued despite valid credentials"

    async def test_two_users_do_not_share_an_authenticated_budget(
        self, client: AsyncClient, valid_user_payload: dict
    ):
        settings = Settings(RATE_LIMIT_METADATA_REQUESTS=3, RATE_LIMIT_METADATA_WINDOW_SECONDS=60)
        fake = FakeRedisClient()
        app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(
            fake, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX)
        )
        try:
            tokens = []
            for email in ("a@nimbusfs.io", "b@nimbusfs.io"):
                payload = {**valid_user_payload, "email": email}
                await client.post("/api/v1/auth/register", json=payload)
                login = await client.post(
                    "/api/v1/auth/login", data={"username": email, "password": payload["password"]}
                )
                tokens.append(login.json()["data"]["access_token"])

            headers_a = {"Authorization": f"Bearer {tokens[0]}"}
            headers_b = {"Authorization": f"Bearer {tokens[1]}"}

            for _ in range(3):
                await client.get("/api/v1/folders", headers=headers_a)
            assert (await client.get("/api/v1/folders", headers=headers_a)).status_code == 429
            assert (await client.get("/api/v1/folders", headers=headers_b)).status_code == 200
        finally:
            app.dependency_overrides.pop(get_rate_limiter, None)

    async def test_api_stays_up_when_redis_dies_and_fail_open_is_on(
        self, authed_client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        """Availability > strict limiting: a Redis outage must not 429 the fleet."""
        settings = Settings(RATE_LIMIT_FAIL_OPEN=True, RATE_LIMIT_METADATA_REQUESTS=1)
        app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(
            fake_redis_client, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX)
        )
        try:
            fake_redis_client.start_failing()
            for _ in range(5):
                assert (await authed_client.get("/api/v1/folders")).status_code == 200
        finally:
            app.dependency_overrides.pop(get_rate_limiter, None)


class TestRateLimitIdentityResolution:
    async def test_an_invalid_bearer_token_falls_back_to_ip_bucketing(
        self, client: AsyncClient, fake_redis_client: FakeRedisClient
    ):
        """A forged token must not let a caller escape into a fresh bucket."""
        settings = Settings(RATE_LIMIT_LOGIN_REQUESTS=2, RATE_LIMIT_LOGIN_WINDOW_SECONDS=60)
        app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(
            fake_redis_client, settings, CacheKeyBuilder(settings.CACHE_KEY_PREFIX)
        )
        try:
            data = {"username": "nobody@nimbusfs.io", "password": "whatever"}
            for index in range(2):
                await client.post(
                    "/api/v1/auth/login",
                    data=data,
                    headers={"Authorization": f"Bearer forged-token-{index}"},
                )
            blocked = await client.post(
                "/api/v1/auth/login", data=data, headers={"Authorization": "Bearer forged-token-999"}
            )
            assert blocked.status_code == 429
        finally:
            app.dependency_overrides.pop(get_rate_limiter, None)

    async def test_bucket_key_shape_is_namespaced_per_category_and_identity(self):
        keys = CacheKeyBuilder("nimbusfs")
        identity = f"user:{uuid.uuid4()}"
        assert keys.rate_limit("login", identity) == f"nimbusfs:ratelimit:login:{identity}"
        assert keys.rate_limit("login", identity) != keys.rate_limit("search", identity)
