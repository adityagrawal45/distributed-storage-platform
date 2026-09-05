"""
Distributed rate limiting via an atomic Redis Lua token bucket (Phase 7).

This replaces the Phase 4 no-op placeholder that previously lived in
`app/middleware/rate_limit.py`. That module now only decorates responses
with the `X-RateLimit-*` headers this limiter produces; all enforcement
happens here, invoked as a FastAPI dependency (see
`app/dependencies/rate_limit.py`).

Algorithm choice, and why not the alternatives
----------------------------------------------
Four algorithms were considered:

**Fixed window counter** (`INCR key:<minute>`, expire after the window).
Cheapest and simplest, and wrong at the boundary: a client can send the
full budget in the last instant of one window and the full budget again in
the first instant of the next, achieving 2x the intended rate in a
sub-second span. For a login endpoint — where the whole point is throttling
credential stuffing — that doubling lands exactly where it hurts.

**Sliding window log** (a sorted set of request timestamps, trim then
count). Exactly accurate, and memory-linear in the request rate: every
single request stores a member for the duration of the window. At 300
req/min/user across many users that is a lot of Redis memory to buy
precision nobody can perceive, plus O(log N) trims on every call.

**Sliding window counter** (weighted blend of the current and previous
fixed windows). A good middle ground — bounded memory, no boundary
doubling — but it is an *approximation* of the rate, and it cannot express
"burst" separately from "sustained rate", which is exactly the distinction
an API wants: a client legitimately fires 20 metadata reads to paint a
screen, then goes quiet.

**Token bucket** (chosen). Two numbers per key (`tokens`, `last refill
timestamp`), O(1) memory and O(1) time, and it models burst and sustained
rate as separate, tunable parameters: `capacity` is the maximum burst,
`capacity / window` is the sustained refill rate. It also yields an exact,
honest `Retry-After` for free — the time until enough tokens accrue — where
the counter approaches can only guess. That is worth a lot for client
behavior: a client told precisely when to come back does not poll.

Atomicity
---------
Read-modify-write on a shared counter from N replicas is a lost-update
race: two pods can both read "1 token left" and both allow. Redis is
single-threaded and executes a Lua script atomically, so the entire
refill-check-decrement sequence below happens as one indivisible
operation. Doing it with WATCH/MULTI/EXEC would need optimistic-retry
loops on contention — i.e. the most retries exactly when the limiter is
hottest. Two script round trips are also worse than one. So: Lua.

Failure policy
--------------
Governed by `Settings.RATE_LIMIT_FAIL_OPEN` (default **true**):

- **fail-open**: Redis unreachable -> allow the request, log at ERROR with
  `rate_limit_degraded`. Recommended and the default for NimbusFS. This is
  a file-storage platform whose users are mid-upload; making a Redis blip
  into a fleet-wide 429 storm turns a degraded dependency into a total
  outage. Rate limiting is abuse mitigation, not an authorization control,
  and it is not the last line of defense (the LB and GCP's own edge
  protections sit in front).
- **fail-closed**: set the flag false where abuse protection genuinely
  outranks availability (e.g. a public unauthenticated tier). The code
  path is implemented and tested, not merely described.

Either way the event is logged loudly. A limiter that has been silently
allowing everything for a week is indistinguishable from having no limiter.

Observability
-------------
`rate_limit_allowed` (debug), `rate_limit_rejected` (warning), and
`rate_limit_degraded` (error) carry `category`, `identity_type`,
`identity_hash`, `limit`, `remaining`, `retry_after_seconds`, and
`duration_ms`. The identity is HASHED, never logged raw: it is either a
user ID or a client IP, both of which are personal data that has no
business sitting in a log aggregator. The hash is stable, so an operator
can still correlate "this same caller keeps getting limited" across lines.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as redis

from app.core.cache.keys import CacheKeyBuilder
from app.core.config.settings import Settings
from app.core.metrics import RATE_LIMIT_DECISIONS_TOTAL, safe_call
from app.logging.logger import get_logger

logger = get_logger(__name__)

# The leading marker comment is load-bearing for the test fake
# (tests/fakes/fake_redis.py dispatches on it) and is genuinely useful in
# production too: `SCRIPT LOAD`ed bodies show up in Redis's slowlog, and a
# named script is far easier to identify there than an anonymous blob.
TOKEN_BUCKET_SCRIPT = """
-- nimbusfs:token_bucket:v1
-- KEYS[1] = bucket key
-- ARGV[1] = capacity (max burst, tokens)
-- ARGV[2] = refill rate (tokens per second)
-- ARGV[3] = now (unix millis, supplied by the caller — see note below)
-- ARGV[4] = tokens requested (normally 1)
-- ARGV[5] = key TTL (millis)
-- returns { allowed(0|1), remaining(int), retry_after_ms(int) }
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local state = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(state[1])
local ts = tonumber(state[2])

if tokens == nil or ts == nil then
    tokens = capacity
    ts = now_ms
end

local elapsed_ms = now_ms - ts
if elapsed_ms < 0 then
    elapsed_ms = 0
end

tokens = tokens + (elapsed_ms * refill_rate / 1000.0)
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
local retry_after_ms = 0

if tokens >= requested then
    allowed = 1
    tokens = tokens - requested
else
    local deficit = requested - tokens
    retry_after_ms = math.ceil((deficit / refill_rate) * 1000.0)
end

redis.call("HSET", key, "tokens", tokens, "ts", now_ms)
redis.call("PEXPIRE", key, ttl_ms)

return { allowed, math.floor(tokens), retry_after_ms }
"""


class RateLimitCategory(str, Enum):
    """
    Route categories with independent budgets.

    Independent on purpose: a user who exhausts their search budget must
    still be able to finish an in-flight upload. One global budget per user
    would let any single noisy endpoint starve every other one.
    """

    LOGIN = "login"
    REGISTER = "register"
    # Phase 10: `/auth/refresh` was previously unmetered even though it
    # is, like login/register, reachable without a prior successful
    # authentication (only a possibly-stolen refresh token is needed) —
    # a real gap found during the Phase 10 security audit, not part of
    # the original Phase 7 design. Given its own budget rather than
    # reusing LOGIN's: a legitimate client refreshing normally as
    # access tokens expire has a different natural request rate than a
    # login form.
    REFRESH = "refresh"
    METADATA = "metadata"
    UPLOAD_INITIATE = "upload_initiate"
    UPLOAD_COMPLETE = "upload_complete"
    SEARCH = "search"
    DEFAULT = "default"


@dataclass(frozen=True)
class RateLimitRule:
    """`requests` per `window_seconds`, as a token bucket."""

    requests: int
    window_seconds: int

    @property
    def capacity(self) -> float:
        return float(self.requests)

    @property
    def refill_rate_per_second(self) -> float:
        return self.requests / self.window_seconds if self.window_seconds > 0 else float(self.requests)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    category: RateLimitCategory
    degraded: bool = False  # True when Redis failed and the policy decided the outcome


def _rules_from_settings(settings: Settings) -> dict[RateLimitCategory, RateLimitRule]:
    return {
        RateLimitCategory.LOGIN: RateLimitRule(
            settings.RATE_LIMIT_LOGIN_REQUESTS, settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS
        ),
        RateLimitCategory.REGISTER: RateLimitRule(
            settings.RATE_LIMIT_REGISTER_REQUESTS, settings.RATE_LIMIT_REGISTER_WINDOW_SECONDS
        ),
        RateLimitCategory.REFRESH: RateLimitRule(
            settings.RATE_LIMIT_REFRESH_REQUESTS, settings.RATE_LIMIT_REFRESH_WINDOW_SECONDS
        ),
        RateLimitCategory.METADATA: RateLimitRule(
            settings.RATE_LIMIT_METADATA_REQUESTS, settings.RATE_LIMIT_METADATA_WINDOW_SECONDS
        ),
        RateLimitCategory.UPLOAD_INITIATE: RateLimitRule(
            settings.RATE_LIMIT_UPLOAD_INITIATE_REQUESTS, settings.RATE_LIMIT_UPLOAD_INITIATE_WINDOW_SECONDS
        ),
        RateLimitCategory.UPLOAD_COMPLETE: RateLimitRule(
            settings.RATE_LIMIT_UPLOAD_COMPLETE_REQUESTS, settings.RATE_LIMIT_UPLOAD_COMPLETE_WINDOW_SECONDS
        ),
        RateLimitCategory.SEARCH: RateLimitRule(
            settings.RATE_LIMIT_SEARCH_REQUESTS, settings.RATE_LIMIT_SEARCH_WINDOW_SECONDS
        ),
        RateLimitCategory.DEFAULT: RateLimitRule(
            settings.RATE_LIMIT_DEFAULT_REQUESTS, settings.RATE_LIMIT_DEFAULT_WINDOW_SECONDS
        ),
    }


class RateLimiter:
    """
    Redis-backed distributed token-bucket limiter.

    Shared across every replica by construction: the bucket state lives in
    Redis, so N pods enforce one budget rather than N budgets — the reason
    an in-process limiter (`slowapi`, a local dict) is not usable here at
    all once the deployment is horizontally scaled (Phase 4/5).
    """

    def __init__(
        self,
        client: redis.Redis,
        settings: Settings,
        keys: CacheKeyBuilder,
    ):
        self._client = client
        self._keys = keys
        self._rules = _rules_from_settings(settings)
        self._enabled = settings.RATE_LIMIT_ENABLED
        self._fail_open = settings.RATE_LIMIT_FAIL_OPEN

    @property
    def enabled(self) -> bool:
        return self._enabled

    def rule_for(self, category: RateLimitCategory) -> RateLimitRule:
        return self._rules.get(category, self._rules[RateLimitCategory.DEFAULT])

    @staticmethod
    def _identity_hash(identity: str) -> str:
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    async def check(
        self,
        category: RateLimitCategory,
        identity: str,
        *,
        identity_type: str = "unknown",
        cost: int = 1,
    ) -> RateLimitResult:
        """
        Consumes `cost` tokens for `identity` in `category`.

        Never raises: the caller (a FastAPI dependency) decides what to do
        with a `not allowed` result. Returns `degraded=True` when the
        outcome came from the failure policy rather than from actual
        bucket state.
        """
        rule = self.rule_for(category)

        if not self._enabled:
            return RateLimitResult(True, rule.requests, rule.requests, 0, category)

        key = self._keys.rate_limit(category.value, identity)
        started = time.perf_counter()

        # `now` is supplied by the *caller*, not read inside Lua, for two
        # reasons: `redis.call("TIME")` makes a script non-deterministic
        # (a problem for replication in older Redis versions and for
        # `SCRIPT` semantics generally), and passing it keeps the script
        # trivially testable. Clock skew between replicas is bounded by
        # NTP and, at worst, shifts a bucket's refill by milliseconds.
        now_ms = int(time.time() * 1000)
        # Idle buckets should evict themselves rather than accumulate one
        # key per user forever: 2x the window is long enough that an
        # actively-limited caller's bucket never disappears mid-throttle,
        # and short enough that a one-off visitor costs nothing for long.
        ttl_ms = max(1000, int(rule.window_seconds * 2 * 1000))

        try:
            raw = await self._client.eval(
                TOKEN_BUCKET_SCRIPT,
                1,
                key,
                str(rule.capacity),
                str(rule.refill_rate_per_second),
                str(now_ms),
                str(cost),
                str(ttl_ms),
            )
            allowed = bool(int(raw[0]))
            remaining = int(raw[1])
            retry_after_ms = int(raw[2])
        except Exception as exc:
            return self._degrade(category, rule, identity, identity_type, exc, started)

        retry_after_seconds = max(1, -(-retry_after_ms // 1000)) if not allowed else 0

        safe_call(
            lambda: RATE_LIMIT_DECISIONS_TOTAL.labels(
                category=category.value, result="allowed" if allowed else "rejected"
            ).inc(),
            operation="rate_limit_decisions_total_inc",
        )

        if allowed:
            logger.debug(
                "rate_limit_allowed",
                category=category.value,
                identity_type=identity_type,
                identity_hash=self._identity_hash(identity),
                limit=rule.requests,
                remaining=remaining,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                result="allowed",
            )
        else:
            logger.warning(
                "rate_limit_rejected",
                category=category.value,
                identity_type=identity_type,
                identity_hash=self._identity_hash(identity),
                limit=rule.requests,
                window_seconds=rule.window_seconds,
                remaining=remaining,
                retry_after_seconds=retry_after_seconds,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                result="rejected",
            )

        return RateLimitResult(
            allowed=allowed,
            limit=rule.requests,
            remaining=max(0, remaining),
            retry_after_seconds=retry_after_seconds,
            category=category,
        )

    def _degrade(
        self,
        category: RateLimitCategory,
        rule: RateLimitRule,
        identity: str,
        identity_type: str,
        exc: BaseException,
        started: float,
    ) -> RateLimitResult:
        """Applies the configured fail-open/fail-closed policy, loudly."""
        safe_call(
            lambda: RATE_LIMIT_DECISIONS_TOTAL.labels(
                category=category.value, result="degraded_open" if self._fail_open else "degraded_closed"
            ).inc(),
            operation="rate_limit_decisions_total_inc",
        )
        logger.error(
            "rate_limit_degraded",
            category=category.value,
            identity_type=identity_type,
            identity_hash=self._identity_hash(identity),
            error_type=type(exc).__name__,
            error=str(exc),
            fail_open=self._fail_open,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            result="allowed_unlimited" if self._fail_open else "rejected_fail_closed",
        )
        if self._fail_open:
            return RateLimitResult(True, rule.requests, rule.requests, 0, category, degraded=True)
        return RateLimitResult(False, rule.requests, 0, rule.window_seconds, category, degraded=True)

    async def reset(self, category: RateLimitCategory, identity: str) -> bool:
        """
        Drops a bucket, restoring full capacity.

        Exists for operational use (an operator un-throttling a
        false-positive) and for tests; never called on the request path.
        """
        try:
            await self._client.delete(self._keys.rate_limit(category.value, identity))
            return True
        except Exception as exc:
            logger.error("rate_limit_reset_failed", category=category.value, error=str(exc))
            return False

    async def peek(self, category: RateLimitCategory, identity: str) -> RateLimitResult:
        """Reports remaining budget without consuming a token (`cost=0`)."""
        return await self.check(category, identity, identity_type="peek", cost=0)
