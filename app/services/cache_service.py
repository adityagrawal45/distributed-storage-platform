"""
The single gateway between NimbusFS and Redis-as-a-cache (Phase 7).

Why one service instead of `await redis.get(...)` wherever it is needed
----------------------------------------------------------------------
Scattering raw Redis calls across services and route handlers means every
one of those call sites has to independently get right: key construction,
serialization, TTL selection, size limits, error handling, and logging.
In practice they never all do — and the ones that get error handling
wrong turn a *cache* outage into an *application* outage, which is the
single most common way a caching layer makes a system less reliable than
it was without one. So: every cache read/write in this codebase goes
through this class, and `redis.asyncio` is imported by exactly three
modules (this one, `app/database/redis.py`, and the rate limiter).

The degradation contract
------------------------
**Postgres is authoritative. Redis is disposable.** Every method here
catches every Redis exception, logs it at WARNING/ERROR with structured
context, and returns the "as if the cache did not exist" answer:

    get()        -> None            (a miss)
    set()        -> False           (write skipped)
    delete()     -> 0               (nothing removed)
    exists()     -> False
    increment()  -> None
    get_or_set() -> loader() result (straight from Postgres)

Silence and fallback are different things: **nothing is ever swallowed
without a log line.** A cache that has been failing every request for a
week while the app quietly serves from Postgres at 10x the latency is a
much worse incident than a loud one.

The one documented exception to "never raise" is inside `get_or_set`'s
stampede lock: a Redis failure *during lock coordination* does not raise
either, it degrades to "everyone reads through to Postgres", because the
lock is a performance optimization, not a correctness mechanism. If it
were a correctness mechanism it would have to raise — see
`DistributedLockService.guard`, which does exactly that for the call
sites where exclusivity is not optional.

Observability
-------------
Every operation emits a structured structlog event —
`cache_hit` / `cache_miss` / `cache_set` / `cache_delete` /
`cache_error` / `cache_skipped_too_large` / `cache_stampede_*` — with
`operation`, `cache_key`, `duration_ms`, and `result`. These are
deliberately shaped as flat key/value pairs with stable event names and a
numeric `duration_ms`, so a future metrics pipeline can turn them into
counters and histograms by scraping the log stream (or by swapping
`logger.info` for a metrics client at these exact call sites) without
touching any of the surrounding logic. A full Prometheus/OpenTelemetry
stack is explicitly out of scope for this phase — structured logs only.
`request_id`/`correlation_id`/`trace_id`/`server_id` are NOT passed
explicitly: `RequestContextMiddleware` binds them into structlog's
contextvars for the whole request, so they land on these lines
automatically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis

from app.core.cache.keys import CacheKeyBuilder
from app.core.metrics import CACHE_OPERATIONS_TOTAL, safe_call
from app.core.cache.policy import CacheEntity, CachePolicy
from app.core.cache.serializer import CacheSerializer
from app.core.distributed_lock import DistributedLockFactory
from app.exceptions.custom_exceptions import CacheSerializationError
from app.logging.logger import get_logger

logger = get_logger(__name__)

# Sentinel distinguishing "cached value is literally None/null" from
# "not in the cache". Without it, caching a legitimately-null result
# (e.g. an empty breadcrumb) would re-hit Postgres forever.
_MISS = object()


class CacheService:
    """
    Cache-aside orchestration over a Redis client.

    Constructed per request via DI (cheap — it holds references, not
    connections; the connection pool is process-wide, see
    `app/database/redis.py`).
    """

    def __init__(
        self,
        client: redis.Redis,
        policy: CachePolicy,
        keys: CacheKeyBuilder,
        *,
        enabled: bool = True,
        lock_factory: DistributedLockFactory | None = None,
    ):
        self._client = client
        self._policy = policy
        self._keys = keys
        self._enabled = enabled
        self._lock_factory = lock_factory
        # Per-instance (per-request) counters, used by tests and by the
        # request-completion log line to report cache effectiveness
        # without a metrics backend.
        self.hits = 0
        self.misses = 0
        self.errors = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def keys(self) -> CacheKeyBuilder:
        return self._keys

    @property
    def policy(self) -> CachePolicy:
        return self._policy

    def ttl_for(self, entity: CacheEntity) -> int:
        return self._policy.ttl_for(entity)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    @staticmethod
    def _record_metric(operation: str, result: str) -> None:
        """
        Phase 11: the exact same event shapes this class already logs
        (`cache_hit`/`cache_miss`/`cache_set`/... — see the module
        docstring's "Observability" section, written back in Phase 7)
        also become a bounded-cardinality counter here. `operation` and
        `result` are both small fixed enums of literal strings this
        class itself chooses — never a cache key — so cardinality stays
        bounded regardless of how many distinct keys exist.
        """
        safe_call(
            lambda: CACHE_OPERATIONS_TOTAL.labels(operation=operation, result=result).inc(),
            operation="cache_operations_total_inc",
        )

    def _on_error(self, operation: str, cache_key: str, exc: BaseException, started: float) -> None:
        """Single funnel for every Redis failure — one log shape, always emitted."""
        self.errors += 1
        self._record_metric(operation, "error")
        logger.error(
            "cache_error",
            operation=operation,
            cache_key=cache_key,
            cache_key_hash=CacheKeyBuilder.redact(cache_key),
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=self._elapsed_ms(started),
            result="degraded_to_source",
        )

    # ------------------------------------------------------------------
    # Primitive operations
    # ------------------------------------------------------------------
    async def get(self, key: str) -> Any | None:
        """Returns the decoded payload, or None for a miss/error/undecodable entry."""
        if not self._enabled:
            return None

        started = time.perf_counter()
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            self._on_error("get", key, exc, started)
            return None

        hit, payload = CacheSerializer.decode(raw)
        if not hit:
            self.misses += 1
            self._record_metric("get", "miss")
            logger.debug(
                "cache_miss",
                operation="get",
                cache_key=key,
                duration_ms=self._elapsed_ms(started),
                # `stale_schema` distinguishes "key absent" from "key
                # present but written by an incompatible build" — the
                # latter is expected during a rolling deploy and should
                # not be read as a cache-effectiveness problem.
                reason="absent" if raw is None else "stale_schema",
                result="miss",
            )
            return None

        self.hits += 1
        self._record_metric("get", "hit")
        logger.debug(
            "cache_hit",
            operation="get",
            cache_key=key,
            duration_ms=self._elapsed_ms(started),
            result="hit",
        )
        return payload

    async def set(self, key: str, value: Any, ttl_seconds: int) -> bool:
        """
        Writes `value` under `key` with an absolute TTL.

        Returns False (having logged) rather than raising if the value is
        unserializable, oversized, write-guarded, or Redis is unavailable.
        A cache write failing must never fail the request that triggered
        it — the caller already has the real answer in hand.
        """
        if not self._enabled:
            return False

        started = time.perf_counter()

        try:
            encoded = CacheSerializer.encode(value)
        except CacheSerializationError as exc:
            self.errors += 1
            logger.error(
                "cache_error",
                operation="set",
                cache_key=key,
                error_type=type(exc).__name__,
                error=exc.detail,
                duration_ms=self._elapsed_ms(started),
                result="write_skipped",
            )
            return False

        size_bytes = len(encoded.encode("utf-8"))
        if size_bytes > self._policy.max_value_bytes:
            logger.warning(
                "cache_skipped_too_large",
                operation="set",
                cache_key=key,
                size_bytes=size_bytes,
                max_value_bytes=self._policy.max_value_bytes,
                duration_ms=self._elapsed_ms(started),
                result="write_skipped",
            )
            return False

        if await self._is_write_guarded(key):
            logger.info(
                "cache_write_guarded",
                operation="set",
                cache_key=key,
                duration_ms=self._elapsed_ms(started),
                result="write_skipped",
            )
            return False

        try:
            await self._client.set(key, encoded, ex=ttl_seconds)
        except Exception as exc:
            self._on_error("set", key, exc, started)
            return False

        self._record_metric("set", "written")
        logger.debug(
            "cache_set",
            operation="set",
            cache_key=key,
            ttl_seconds=ttl_seconds,
            size_bytes=size_bytes,
            duration_ms=self._elapsed_ms(started),
            result="written",
        )
        return True

    async def delete(self, *keys: str) -> int:
        """Deletes one or more keys. Returns how many actually existed (0 on error)."""
        if not self._enabled or not keys:
            return 0

        started = time.perf_counter()
        try:
            removed = int(await self._client.delete(*keys))
        except Exception as exc:
            self._on_error("delete", ",".join(keys), exc, started)
            return 0

        self._record_metric("delete", "deleted")
        logger.debug(
            "cache_delete",
            operation="delete",
            cache_key=keys[0] if len(keys) == 1 else None,
            key_count=len(keys),
            removed=removed,
            duration_ms=self._elapsed_ms(started),
            result="deleted",
        )
        return removed

    async def exists(self, key: str) -> bool:
        if not self._enabled:
            return False
        started = time.perf_counter()
        try:
            return bool(await self._client.exists(key))
        except Exception as exc:
            self._on_error("exists", key, exc, started)
            return False

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        """Resets an existing key's TTL. False if the key is gone or Redis failed."""
        if not self._enabled:
            return False
        started = time.perf_counter()
        try:
            return bool(await self._client.expire(key, ttl_seconds))
        except Exception as exc:
            self._on_error("expire", key, exc, started)
            return False

    async def increment(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int | None:
        """
        Atomic counter increment (`INCRBY`), optionally setting a TTL the
        first time the counter comes into existence.

        The TTL is applied only when the post-increment value equals
        `amount` — i.e. this call created the counter. Re-applying it on
        every increment would make a busy counter immortal, which is the
        classic way a "rolling window" counter silently becomes an
        all-time counter.

        Returns None on failure. A caller counting something must treat
        None as "unknown", never as zero.
        """
        if not self._enabled:
            return None
        started = time.perf_counter()
        try:
            value = int(await self._client.incrby(key, amount))
            if ttl_seconds is not None and value == amount:
                await self._client.expire(key, ttl_seconds)
            return value
        except Exception as exc:
            self._on_error("increment", key, exc, started)
            return None

    async def scan_keys(self, pattern: str, *, limit: int = 5000) -> list[str]:
        """
        Collects keys matching a glob pattern using SCAN — never KEYS.

        `KEYS` is O(N) over the entire keyspace and blocks Redis's single
        command thread for the duration, which on a production instance
        with millions of keys is a self-inflicted outage. SCAN is
        incremental and cursor-based. `limit` bounds the worst case so a
        pathological pattern cannot make one request iterate forever.
        """
        if not self._enabled:
            return []
        started = time.perf_counter()
        found: list[str] = []
        try:
            async for key in self._client.scan_iter(match=pattern, count=250):
                found.append(key.decode("utf-8") if isinstance(key, bytes) else key)
                if len(found) >= limit:
                    logger.warning(
                        "cache_scan_truncated",
                        operation="scan_keys",
                        pattern=pattern,
                        limit=limit,
                        duration_ms=self._elapsed_ms(started),
                    )
                    break
        except Exception as exc:
            self._on_error("scan_keys", pattern, exc, started)
            return []
        return found

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------
    async def invalidate(self, *keys: str) -> int:
        """
        Deletes keys and (if `CACHE_WRITE_GUARD_SECONDS` > 0) plants a
        short-lived tombstone that suppresses re-population of each key.

        The tombstone closes a real race: a writer's DELETE lands, a
        concurrent reader misses, reads the *not-yet-committed* old row
        from Postgres, and writes it back — leaving a stale entry that
        outlives the write by a full TTL. Because the guard also leaves
        the key deliberately cold for its duration, it is off by default;
        see `Settings.CACHE_WRITE_GUARD_SECONDS` and the race analysis in
        docs/PHASE_7_REDIS_DESIGN.md.
        """
        if not self._enabled or not keys:
            return 0

        removed = await self.delete(*keys)

        guard_seconds = self._policy.write_guard_seconds
        if guard_seconds > 0:
            for key in keys:
                started = time.perf_counter()
                try:
                    await self._client.set(
                        self._keys.write_guard(key), "1", px=max(1, int(guard_seconds * 1000))
                    )
                except Exception as exc:
                    self._on_error("write_guard", key, exc, started)

        logger.info(
            "cache_invalidated",
            operation="invalidate",
            key_count=len(keys),
            removed=removed,
            write_guard_seconds=guard_seconds,
            result="invalidated",
        )
        return removed

    async def invalidate_pattern(self, pattern: str) -> int:
        """SCAN-and-delete every key matching `pattern`. Returns the count removed."""
        keys = await self.scan_keys(pattern)
        if not keys:
            return 0
        return await self.invalidate(*keys)

    async def _is_write_guarded(self, key: str) -> bool:
        if self._policy.write_guard_seconds <= 0:
            return False
        started = time.perf_counter()
        try:
            return bool(await self._client.exists(self._keys.write_guard(key)))
        except Exception as exc:
            # Fail *open* for the guard specifically: not being able to
            # check it must not block the write path. The consequence is
            # at worst one stale entry, bounded by TTL.
            self._on_error("write_guard_check", key, exc, started)
            return False

    # ------------------------------------------------------------------
    # Cache-aside with stampede protection
    # ------------------------------------------------------------------
    async def get_or_set(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl_seconds: int,
        *,
        entity: CacheEntity | None = None,
        cacheable: Callable[[Any], bool] | None = None,
    ) -> Any:
        """
        The cache-aside primitive, with single-flight stampede protection.

        Plain cache-aside has a well-known failure mode: when a hot key
        expires (or is invalidated) under load, *every* concurrent request
        misses simultaneously and they all pile onto Postgres at once —
        a "cache stampede" or "thundering herd". At 500 rps against one
        key that is 500 identical queries where 1 was needed, and it tends
        to happen precisely when the system is already busy.

        The chosen mitigation is **single-flight with a bounded wait**:

            1. GET. On a hit, done — no locking on the hot path at all.
            2. On a miss, try (non-blocking) to acquire a short-TTL
               Redis lock keyed on this cache key.
            3. **Winner**: re-GET (someone may have populated between our
               miss and our acquire — this recheck is what makes the
               pattern correct rather than merely lucky), then run
               `loader()` against Postgres, write the result, release.
            4. **Losers**: poll the cache every
               `CACHE_STAMPEDE_POLL_INTERVAL_SECONDS` for at most
               `CACHE_STAMPEDE_WAIT_SECONDS`. If the winner publishes in
               that window, they get a cache hit and never touch Postgres.
               If it does not, they **read through to Postgres themselves**
               and return normally.

        Step 4's fallthrough is the important design choice: a request is
        NEVER blocked indefinitely waiting for another request's work. A
        pattern that waits unboundedly converts one slow query into
        thread-pool exhaustion and then a total outage — strictly worse
        than the stampede it was preventing. The guarantee here is
        deliberately "far fewer DB hits than requests", not "exactly one";
        buying the stronger guarantee would cost unbounded coupling
        between unrelated requests.

        The lock TTL (`CACHE_STAMPEDE_LOCK_TTL_SECONDS`, 5s) also bounds
        the crashed-winner case: if the populating replica dies mid-query,
        the lock self-expires and the next requester becomes the winner.

        `cacheable` is an optional predicate letting a caller refuse to
        cache particular results (used by search, which declines to cache
        oversized result pages).
        """
        cached = await self.get(key)
        if cached is not None:
            return cached

        if not (self._enabled and self._policy.stampede_protection_enabled and self._lock_factory is not None):
            value = await loader()
            await self._maybe_set(key, value, ttl_seconds, cacheable)
            return value

        lock = self._lock_factory.lock(
            self._keys.stampede_lock(key), ttl_seconds=self._policy.stampede_lock_ttl_seconds
        )

        started = time.perf_counter()
        try:
            acquired = await lock.acquire()
        except Exception as exc:
            # Coordination failed. This is explicitly non-fatal: the lock
            # is a performance optimization, so we degrade to plain
            # cache-aside (every request reads through) rather than
            # failing the request. Logged, never silent.
            self._on_error("stampede_lock_acquire", key, exc, started)
            value = await loader()
            await self._maybe_set(key, value, ttl_seconds, cacheable)
            return value

        if acquired:
            try:
                # Double-check: a winner from a moment ago may have
                # published while we were acquiring.
                recheck = await self.get(key)
                if recheck is not None:
                    return recheck

                logger.debug(
                    "cache_stampede_leader",
                    operation="get_or_set",
                    cache_key=key,
                    entity=entity.value if entity else None,
                    result="loading_from_source",
                )
                value = await loader()
                await self._maybe_set(key, value, ttl_seconds, cacheable)
                return value
            finally:
                await lock.release()

        # Follower path: bounded wait, then read through regardless.
        deadline = time.perf_counter() + self._policy.stampede_wait_seconds
        polls = 0
        while time.perf_counter() < deadline:
            await asyncio.sleep(self._policy.stampede_poll_interval_seconds)
            polls += 1
            published = await self.get(key)
            if published is not None:
                logger.debug(
                    "cache_stampede_follower_served",
                    operation="get_or_set",
                    cache_key=key,
                    polls=polls,
                    duration_ms=self._elapsed_ms(started),
                    result="served_from_leader",
                )
                return published

        logger.info(
            "cache_stampede_follower_read_through",
            operation="get_or_set",
            cache_key=key,
            polls=polls,
            waited_seconds=self._policy.stampede_wait_seconds,
            duration_ms=self._elapsed_ms(started),
            result="read_through_to_source",
        )
        value = await loader()
        await self._maybe_set(key, value, ttl_seconds, cacheable)
        return value

    async def _maybe_set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
        cacheable: Callable[[Any], bool] | None,
    ) -> None:
        if value is None:
            # Null results are not cached this phase. Doing so ("negative
            # caching") would defend against a repeated-lookup-of-a-
            # nonexistent-ID attack, but it also means a just-created
            # resource can 404 for a full TTL. Deliberately out of scope;
            # noted rather than silently omitted.
            return
        if cacheable is not None and not cacheable(value):
            return
        await self.set(key, value, ttl_seconds)
