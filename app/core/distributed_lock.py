"""
Distributed lock interface (infrastructure only — see README).

Design decisions:
- Unlike the cache layer, a lock's whole purpose is mutual exclusion, so
  it fails CLOSED: if Redis is unreachable, `acquire()` returns `False`
  (or `LockAcquisitionException` under the context-manager form) rather
  than pretending the lock was granted. Failing open here would silently
  remove the mutual-exclusion guarantee at exactly the moment (a Redis
  outage) when instances are most likely to be racing each other in
  confusing ways.
- Acquire: `SET key token NX PX=ttl_ms` — a single atomic Redis command.
  `token` is a random UUID4 unique to this acquisition, NOT the caller's
  identity, so a lock can never be released by anyone other than the
  holder that acquired it (or by TTL expiry).
- Release: a `WATCH`/`MULTI`/`EXEC` optimistic transaction that only
  issues `DEL` if the stored value still matches our token. This is the
  portable equivalent of the classic Redlock "compare-and-delete Lua
  script" — chosen over `EVAL` because it works unchanged against both
  real Redis and `fakeredis` in tests (fakeredis has no Lua/EVAL
  support), and because it's one fewer thing (a Lua script) to review for
  correctness.
- TTL is mandatory and always set (`DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS`
  by default). A crashed lock holder must never be able to deadlock a
  resource forever — the lock self-heals via expiry even if `release()`
  is never called (process killed, node evicted, etc.).
- No auto-renewal/"watchdog" thread in this phase: callers doing
  long-running work under a lock should call `extend()` periodically, or
  (simpler, and preferred) keep the critical section short and size the
  TTL to comfortably cover it. A background renewal thread is exactly the
  kind of local, easy-to-get-subtly-wrong state this phase avoids
  introducing before it's actually needed.
- Not used by any endpoint yet in Phase 4 (there is no cross-instance
  critical section in the current API surface) — this is the interface a
  future phase's "two instances must not process the same background job
  twice" or "only one instance replaces this file's bytes at a time"
  logic would use, wrapped as `async with DistributedLock(key):`.
"""

import uuid

import redis.asyncio as redis_asyncio

from app.core.config import get_settings
from app.database import redis as redis_db
from app.exceptions.custom_exceptions import LockAcquisitionException
from app.logging.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

LOCK_KEY_PREFIX = "lock:"


class DistributedLock:
    """
    Redis-backed mutual-exclusion lock.

    Usage:
        lock = DistributedLock("files:replace:<file_id>")
        async with lock:
            ...critical section...
        # lock released automatically on exit, even if an exception was raised

    Or, for a non-raising acquire attempt:
        lock = DistributedLock("files:replace:<file_id>")
        if await lock.acquire():
            try:
                ...
            finally:
                await lock.release()
    """

    def __init__(self, resource: str, ttl_seconds: int | None = None):
        self.key = f"{LOCK_KEY_PREFIX}{resource}"
        self.ttl_seconds = ttl_seconds or settings.DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS
        self._token: str | None = None

    async def acquire(self) -> bool:
        token = uuid.uuid4().hex
        client = redis_db.get_redis_client()
        try:
            acquired = await client.set(self.key, token, nx=True, px=int(self.ttl_seconds * 1000))
        except redis_asyncio.RedisError as exc:
            logger.warning("distributed_lock_acquire_failed", key=self.key, error=str(exc))
            return False
        finally:
            await client.aclose()

        if acquired:
            self._token = token
            return True
        return False

    async def release(self) -> bool:
        """Releases the lock only if this instance is still the holder (token match)."""
        if self._token is None:
            return False

        client = redis_db.get_redis_client()
        try:
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(self.key)
                    current = await pipe.get(self.key)
                    pipe.multi()
                    if current == self._token:
                        pipe.delete(self.key)
                        results = await pipe.execute()
                        return bool(results and results[0])
                    await pipe.unwatch()
                    return False
                except redis_asyncio.WatchError:
                    # Someone else mutated the key mid-transaction (e.g. it
                    # already expired and was re-acquired) — safe to treat
                    # as "not ours to release anymore."
                    return False
        except redis_asyncio.RedisError as exc:
            logger.warning("distributed_lock_release_failed", key=self.key, error=str(exc))
            return False
        finally:
            await client.aclose()
            self._token = None

    async def extend(self, additional_seconds: int | None = None) -> bool:
        """Renews the TTL if this instance still holds the lock. For long-running critical sections."""
        if self._token is None:
            return False
        extra = additional_seconds or self.ttl_seconds

        client = redis_db.get_redis_client()
        try:
            current = await client.get(self.key)
            if current != self._token:
                return False
            await client.pexpire(self.key, int(extra * 1000))
            return True
        except redis_asyncio.RedisError as exc:
            logger.warning("distributed_lock_extend_failed", key=self.key, error=str(exc))
            return False
        finally:
            await client.aclose()

    async def __aenter__(self) -> "DistributedLock":
        if not await self.acquire():
            raise LockAcquisitionException()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.release()
