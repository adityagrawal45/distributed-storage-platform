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
"""

import uuid
from types import TracebackType
from typing import Self

import redis.asyncio as redis

from app.exceptions.custom_exceptions import LockAcquisitionException
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

    async def acquire(self) -> bool:
        """Best-effort single attempt (no internal retry/backoff — see `acquire_or_raise`)."""
        acquired = await self._client.set(self._key, self._token, nx=True, px=self._ttl_ms)
        self._held = bool(acquired)
        return self._held

    async def acquire_or_raise(self) -> None:
        if not await self.acquire():
            raise LockAcquisitionException(detail=f"Could not acquire distributed lock '{self._key}'.")

    async def release(self) -> None:
        if not self._held:
            return
        await self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
        self._held = False

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
