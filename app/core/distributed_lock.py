"""
Redis-backed distributed lock (Phase 4).

Design decisions:
- Once there are N interchangeable FastAPI replicas, in-process locks
  (`asyncio.Lock`, a plain dict-based mutex) stop working: two different
  requests hitting two different pods can race on the same logical
  resource with no shared memory to serialize them. Redis, already a
  shared dependency of every replica, is the natural place to coordinate.
- Built on `SET key value NX PX <ttl>` (atomic acquire-with-expiry in a
  single round trip) and a Lua script for release that only deletes the
  key if its value still matches the token we set — this prevents
  replica A from ever releasing a lock that replica B has since
  acquired after A's lock expired (the classic "lost lock" bug).
- The token is a random UUID per acquisition attempt, not e.g. the
  request ID — a lock object must be safe to use for multiple
  acquire/release cycles.
- TTL-based expiry (not a heartbeat/renewal scheme like Redlock) is a
  deliberate simplicity trade-off appropriate for this phase: it bounds
  "how long can a crashed holder block everyone else" at `ttl_seconds`,
  which is sufficient for our actual use case (idempotency-key
  in-flight markers, short critical sections) without the operational
  complexity of a renewal thread. Document this trade-off rather than
  hide it: for a lock that must be held longer than its TTL, extend the
  TTL, don't rely on renewal that doesn't exist here.
- Exposed as an async context manager so call sites read like a normal
  `async with lock:` critical section and can never forget to release.

Phase 7 additions (extending, not replacing, the above):
- `DistributedLock.acquire_with_timeout()` — bounded, jittered retry loop
  for callers that want to *wait* for a lock rather than fail on the
  first contended attempt. Raises `LockAcquisitionTimeout` (a subclass of
  Phase 4's `LockAcquisitionException`, so the existing 409 handler still
  applies unchanged) when the budget is spent. There is deliberately no
  "wait forever" option: an unbounded wait converts lock contention into
  thread/worker exhaustion, which is how a slow subsystem becomes a total
  outage.
- Ownership introspection (`is_held`, `token`, `owns()`), so a caller can
  assert it still holds a lock before doing something destructive rather
  than assuming the TTL has not lapsed underneath it. `release()` /
  `extend()` were already token-checked in Redis via Lua; `owns()` adds
  the *local* half of that check and a strict-mode release that raises
  `LockOwnershipError` instead of silently no-op'ing.
- `DistributedLockService` — a thin, DI-friendly facade over the existing
  `DistributedLockFactory` that adds `guard()`: an async context manager
  which translates Redis *infrastructure* failures during acquire/release
  into a caller-chosen outcome, and logs every acquisition, contention,
  and timeout event as structured data. The lock algorithm itself is
  unchanged — this phase adds ergonomics and observability around it, not
  a second implementation.
- Structured log events (`lock_acquired`, `lock_contended`,
  `lock_acquire_timeout`, `lock_released`, `lock_release_not_owned`,
  `lock_redis_error`) carry `lock_key`, `duration_ms`, and `attempts`, so
  they can be scraped into metrics later without a code change.
"""

import asyncio
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

import redis.asyncio as redis

from app.exceptions.custom_exceptions import (
    DistributedLockError,
    LockAcquisitionException,
    LockAcquisitionTimeout,
    LockOwnershipError,
)
from app.logging.logger import get_logger

logger = get_logger(__name__)

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_EXTEND_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class DistributedLock:
    """
    A single acquire/release cycle for one named resource.

    Not reentrant, not reusable across concurrent `async with` blocks —
    create a fresh instance (or use `DistributedLockFactory.lock(...)`)
    per critical section.
    """

    def __init__(self, client: redis.Redis, key: str, ttl_seconds: float):
        self._client = client
        self._key = f"lock:{key}"
        self._ttl_ms = int(ttl_seconds * 1000)
        self._token = str(uuid.uuid4())
        self._held = False

    # -- introspection (Phase 7) --------------------------------------
    @property
    def key(self) -> str:
        return self._key

    @property
    def token(self) -> str:
        """This acquisition's unique fencing token. Never reused across cycles."""
        return self._token

    @property
    def is_held(self) -> bool:
        """
        Whether THIS object believes it holds the lock.

        Local belief only — the lock may have expired in Redis without
        this process noticing (that is inherent to TTL-based locking, see
        the module docstring). Use `owns()` for the authoritative check.
        """
        return self._held

    async def owns(self) -> bool:
        """
        Authoritative ownership check: asks Redis whether the key still
        carries *our* token. Costs a round trip, so it is opt-in — used
        before irreversible work inside a long critical section, not on
        every operation.
        """
        if not self._held:
            return False
        current = await self._client.get(self._key)
        return current == self._token

    async def acquire(self) -> bool:
        """Best-effort single attempt (no internal retry/backoff — see `acquire_or_raise`)."""
        acquired = await self._client.set(self._key, self._token, nx=True, px=self._ttl_ms)
        self._held = bool(acquired)
        return self._held

    async def acquire_or_raise(self) -> None:
        if not await self.acquire():
            raise LockAcquisitionException(detail=f"Could not acquire distributed lock '{self._key}'.")

    async def acquire_with_timeout(
        self,
        timeout_seconds: float,
        retry_interval_seconds: float = 0.05,
        *,
        raise_on_timeout: bool = True,
    ) -> bool:
        """
        Retries acquisition until `timeout_seconds` elapses (Phase 7).

        Jitters each sleep (`uniform(0.5x, 1.5x)` of the interval) for the
        same reason `retry_async` does: N replicas all waiting on the same
        hot key would otherwise poll in lockstep and hammer Redis in
        synchronized waves.

        Returns True on success. On exhaustion, raises
        `LockAcquisitionTimeout` (409 via the existing Phase 4 handler)
        unless `raise_on_timeout=False`, in which case it returns False so
        a caller with a graceful fallback (like the cache stampede path)
        can take it without exception-handling ceremony.
        """
        started = time.monotonic()
        deadline = started + max(0.0, timeout_seconds)
        attempts = 0

        while True:
            attempts += 1
            if await self.acquire():
                logger.debug(
                    "lock_acquired",
                    lock_key=self._key,
                    attempts=attempts,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "lock_acquire_timeout",
                    lock_key=self._key,
                    attempts=attempts,
                    timeout_seconds=timeout_seconds,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                if raise_on_timeout:
                    raise LockAcquisitionTimeout(
                        detail=f"Timed out after {timeout_seconds}s waiting for distributed lock '{self._key}'."
                    )
                return False

            jittered = min(remaining, retry_interval_seconds * random.uniform(0.5, 1.5))
            await asyncio.sleep(jittered)

    async def release(self, *, strict: bool = False) -> bool:
        """
        Releases the lock if — and only if — Redis still holds our token.

        Returns True if we actually deleted the key. A False return means
        the lock had already expired and (possibly) been taken by someone
        else; that is not an error by default, because the most common
        cause is simply "the critical section outlived its TTL", which the
        TTL exists to bound. `strict=True` raises `LockOwnershipError`
        instead, for call sites where losing the lock mid-section means
        the work they just did may have raced and the caller genuinely
        needs to know.
        """
        if not self._held:
            return False
        released = bool(await self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token))
        self._held = False
        if released:
            logger.debug("lock_released", lock_key=self._key)
        else:
            logger.warning("lock_release_not_owned", lock_key=self._key)
            if strict:
                raise LockOwnershipError(
                    detail=f"Lock '{self._key}' expired or was taken by another holder before release."
                )
        return released

    async def extend(self, ttl_seconds: float) -> bool:
        """Extends the lock's TTL — only succeeds if we still hold it."""
        if not self._held:
            return False
        extended = await self._client.eval(_EXTEND_SCRIPT, 1, self._key, self._token, int(ttl_seconds * 1000))
        return bool(extended)

    async def __aenter__(self) -> Self:
        await self.acquire_or_raise()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.release()


class DistributedLockFactory:
    """
    Thin factory bound to a Redis client, handed out via DI
    (`app/dependencies/providers.py::DistributedLockFactoryDep`) so
    services never construct `DistributedLock` against a raw client
    directly — keeps the default TTL policy centralized.
    """

    def __init__(self, client: redis.Redis, default_ttl_seconds: float):
        self._client = client
        self._default_ttl_seconds = default_ttl_seconds

    def lock(self, key: str, ttl_seconds: float | None = None) -> DistributedLock:
        return DistributedLock(self._client, key, ttl_seconds or self._default_ttl_seconds)


class DistributedLockService:
    """
    DI-friendly facade over `DistributedLockFactory` (Phase 7).

    Deliberately a thin wrapper, NOT a second lock implementation: the
    `SET NX PX` + Lua-checked-release algorithm in `DistributedLock` above
    already covers unique tokens, TTL, safe acquisition, safe release and
    ownership validation, so duplicating it would create two things to
    keep correct. What was genuinely missing — and is what this class adds
    — is a single place that:

      1. applies the configured acquire timeout/retry interval, so call
         sites stop passing them individually and drifting;
      2. distinguishes the two failure modes that must NOT be conflated:
         *contention* (someone else holds it — a normal, expected
         business outcome -> `LockAcquisitionTimeout` -> 409) versus
         *infrastructure failure* (Redis is unreachable -> the lock cannot
         be reasoned about at all -> `DistributedLockError`, which callers
         may map to 503 or degrade around);
      3. emits consistent structured logs for both.

    Note the asymmetry in `guard()`: a Redis failure at ACQUIRE time is
    fatal to the critical section (we cannot prove exclusivity, so we must
    not proceed), while a Redis failure at RELEASE time is logged and
    swallowed (the work already happened, and the lock's TTL guarantees it
    frees itself). Raising on release would turn a successful operation
    into a client-visible error for no benefit. This mirrors the existing
    `ChunkedUploadService._guarded_lock` reasoning from Phase 6.
    """

    def __init__(
        self,
        factory: DistributedLockFactory,
        *,
        default_timeout_seconds: float,
        retry_interval_seconds: float,
    ):
        self._factory = factory
        self._default_timeout_seconds = default_timeout_seconds
        self._retry_interval_seconds = retry_interval_seconds

    def lock(self, key: str, ttl_seconds: float | None = None) -> DistributedLock:
        return self._factory.lock(key, ttl_seconds)

    async def acquire(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = True,
    ) -> DistributedLock | None:
        """
        Acquires `key`, waiting up to `timeout_seconds`.

        Returns the held lock, or None when the wait was exhausted and
        `raise_on_timeout=False`. Raises `DistributedLockError` if Redis
        itself failed — never silently treats "I could not talk to Redis"
        as "the lock is free", which would defeat the entire point of
        holding one.
        """
        lock = self._factory.lock(key, ttl_seconds)
        timeout = self._default_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            acquired = await lock.acquire_with_timeout(
                timeout,
                self._retry_interval_seconds,
                raise_on_timeout=raise_on_timeout,
            )
        except LockAcquisitionTimeout:
            logger.info("lock_contended", lock_key=lock.key, timeout_seconds=timeout)
            raise
        except (LockAcquisitionException, DistributedLockError):
            raise
        except Exception as exc:  # Redis unreachable / timed out / pool exhausted
            logger.error("lock_redis_error", lock_key=lock.key, phase="acquire", error=str(exc))
            raise DistributedLockError(
                detail="Could not reach the coordination backend to acquire a lock."
            ) from exc
        return lock if acquired else None

    async def release(self, lock: DistributedLock) -> bool:
        """Releases, absorbing (but always logging) a Redis failure — see class docstring."""
        try:
            return await lock.release()
        except Exception as exc:
            logger.error("lock_redis_error", lock_key=lock.key, phase="release", error=str(exc))
            return False

    @asynccontextmanager
    async def guard(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[DistributedLock]:
        """
        `async with lock_service.guard("folder:move:<id>"):` — the normal
        way to use this class. Always releases, including on exception.
        """
        lock = await self.acquire(key, ttl_seconds=ttl_seconds, timeout_seconds=timeout_seconds)
        assert lock is not None  # raise_on_timeout=True guarantees this
        try:
            yield lock
        finally:
            await self.release(lock)
