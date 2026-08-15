"""
In-memory fake of the `redis.asyncio.Redis` surface actually used by
NimbusFS. Mirrors `tests/fakes/fake_gcs.py`'s philosophy: a hand-written
fake that really stores/expires values, not a `Mock`, so tests assert on
real behavior (NX fails once a key exists; a Lua-checked release only
deletes a matching token; a token bucket really refills over time) rather
than "was this method called".

Deliberately implements only what NimbusFS calls — this is not a general
Redis emulator.

Phase 4 surface: ping/set/get/delete/keys/eval/aclose.

Phase 7 additions:
- `exists`, `expire`, `incrby`, `ttl`, `scan_iter`, and hash storage —
  the commands `CacheService` and `RateLimiter` actually issue.
- Real token-bucket semantics in `eval` for the rate limiter's Lua
  script, including time-based refill driven by an injectable clock, so
  "the bucket refills after the window" is a genuine test rather than a
  sleep.
- **Failure injection** (`fail_mode`, `fail_after`, `fail_commands`) so
  Redis-outage behavior — graceful cache degradation, rate-limiter
  fail-open/fail-closed, lock coordination failure — can be tested with
  no real Redis and no flakiness. This is what makes "the app keeps
  serving when Redis dies" an assertion instead of a claim.
"""

from __future__ import annotations

import fnmatch
import math
import time
from collections.abc import AsyncIterator
from typing import Any


class FakeRedisFailure(ConnectionError):
    """
    What the fake raises in failure mode.

    Subclasses `ConnectionError` because that is what `redis.asyncio`
    surfaces for an unreachable server (`redis.exceptions.ConnectionError`
    itself derives from it), so production code paths that catch broad
    `Exception` — which is what every Phase 7 degradation path does — are
    exercised exactly as they would be against a real outage.
    """


class FakeRedisClient:
    def __init__(self, *, clock=None):
        self._store: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._expiry: dict[str, float] = {}
        self.closed = False

        # Injectable monotonic clock so time-dependent behavior (TTL
        # expiry, token-bucket refill) can be driven deterministically by
        # a test instead of by `asyncio.sleep`.
        self._clock = clock or time.monotonic

        # -- failure injection --------------------------------------
        # fail_mode:     when True, matching commands raise.
        # fail_commands: None = every command; otherwise a set of names.
        # fail_after:    let N matching commands succeed first, then fail
        #                (models "Redis dies mid-request", not just
        #                "Redis was already down").
        self.fail_mode = False
        self.fail_commands: set[str] | None = None
        self.fail_after = 0

        # Call counters — used by stampede tests to assert how many times
        # a given command actually reached the "server".
        self.command_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Test controls
    # ------------------------------------------------------------------
    def start_failing(self, *commands: str, after: int = 0) -> None:
        """Turn on failure injection, optionally scoped to named commands."""
        self.fail_mode = True
        self.fail_commands = set(commands) if commands else None
        self.fail_after = after

    def stop_failing(self) -> None:
        self.fail_mode = False
        self.fail_commands = None
        self.fail_after = 0

    def advance(self, seconds: float) -> None:
        """
        Move the fake's clock forward without really sleeping.

        Only meaningful when constructed with a controllable clock; the
        default monotonic clock ignores this (tests that need it use
        `FakeClock` below).
        """
        if hasattr(self._clock, "advance"):
            self._clock.advance(seconds)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record(self, command: str) -> None:
        self.command_counts[command] = self.command_counts.get(command, 0) + 1
        if not self.fail_mode:
            return
        if self.fail_commands is not None and command not in self.fail_commands:
            return
        if self.fail_after > 0:
            self.fail_after -= 1
            return
        raise FakeRedisFailure(f"FakeRedisClient: injected failure on '{command}'")

    def _now(self) -> float:
        return self._clock()

    def _is_expired(self, key: str) -> bool:
        expires_at = self._expiry.get(key)
        return expires_at is not None and self._now() >= expires_at

    def _purge_if_expired(self, key: str) -> None:
        if self._is_expired(key):
            self._store.pop(key, None)
            self._hashes.pop(key, None)
            self._expiry.pop(key, None)

    def _live_keys(self) -> list[str]:
        keys = set(self._store) | set(self._hashes)
        return [k for k in keys if not self._is_expired(k)]

    # ------------------------------------------------------------------
    # String commands
    # ------------------------------------------------------------------
    async def ping(self) -> bool:
        self._record("ping")
        return True

    async def set(
        self,
        name: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool:
        self._record("set")
        self._purge_if_expired(name)

        if nx and name in self._store:
            return False

        self._store[name] = value
        if ex is not None:
            self._expiry[name] = self._now() + ex
        elif px is not None:
            self._expiry[name] = self._now() + (px / 1000)
        else:
            self._expiry.pop(name, None)
        return True

    async def get(self, name: str) -> str | None:
        self._record("get")
        self._purge_if_expired(name)
        return self._store.get(name)

    async def delete(self, *names: str) -> int:
        self._record("delete")
        count = 0
        for name in names:
            self._purge_if_expired(name)
            if name in self._store or name in self._hashes:
                self._store.pop(name, None)
                self._hashes.pop(name, None)
                self._expiry.pop(name, None)
                count += 1
        return count

    async def exists(self, *names: str) -> int:
        self._record("exists")
        count = 0
        for name in names:
            self._purge_if_expired(name)
            if name in self._store or name in self._hashes:
                count += 1
        return count

    async def expire(self, name: str, seconds: int) -> bool:
        self._record("expire")
        self._purge_if_expired(name)
        if name not in self._store and name not in self._hashes:
            return False
        self._expiry[name] = self._now() + seconds
        return True

    async def ttl(self, name: str) -> int:
        """Redis semantics: -2 = no such key, -1 = key with no expiry."""
        self._record("ttl")
        self._purge_if_expired(name)
        if name not in self._store and name not in self._hashes:
            return -2
        if name not in self._expiry:
            return -1
        return max(0, int(math.ceil(self._expiry[name] - self._now())))

    async def incrby(self, name: str, amount: int = 1) -> int:
        self._record("incrby")
        self._purge_if_expired(name)
        current = int(self._store.get(name, "0"))
        current += amount
        self._store[name] = str(current)
        return current

    # `incr` is redis-py's alias for INCRBY with amount=1.
    async def incr(self, name: str, amount: int = 1) -> int:
        return await self.incrby(name, amount)

    async def keys(self, pattern: str = "*") -> list[str]:
        self._record("keys")
        return [k for k in self._live_keys() if fnmatch.fnmatch(k, pattern)]

    async def scan_iter(self, match: str | None = None, count: int | None = None) -> AsyncIterator[str]:
        """
        Async-generator SCAN, matching redis-py's signature.

        Iterates a snapshot of the live keyspace: real SCAN gives no
        snapshot guarantee either, and iterating a live dict while a
        concurrent test coroutine mutates it would raise where real Redis
        would not.
        """
        self._record("scan_iter")
        for key in list(self._live_keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    # ------------------------------------------------------------------
    # Scripting
    # ------------------------------------------------------------------
    async def eval(self, script: str, numkeys: int, *keys_and_args) -> Any:
        """
        Understands exactly the three Lua scripts NimbusFS ships:

        1. `app/core/distributed_lock.py`'s release-if-token-matches
        2. `app/core/distributed_lock.py`'s pexpire-if-token-matches
        3. `app/core/rate_limiter.py`'s token bucket (tagged with the
           `nimbusfs:token_bucket` marker comment)

        Dispatched by content rather than by implementing a Lua
        interpreter. The token bucket is reimplemented here in Python with
        the *same* arithmetic as the Lua, so a test asserting "the 11th
        request in a 10/60s budget is rejected, and it is allowed again
        after the bucket refills" is testing real behavior.
        """
        self._record("eval")

        if "nimbusfs:token_bucket" in script:
            return self._token_bucket(keys_and_args)

        key = keys_and_args[0]
        token = keys_and_args[1]
        self._purge_if_expired(key)
        current = self._store.get(key)

        if current != token:
            return 0

        if "del" in script:
            del self._store[key]
            self._expiry.pop(key, None)
            return 1

        if "pexpire" in script:
            ttl_ms = int(keys_and_args[2])
            self._expiry[key] = self._now() + (ttl_ms / 1000)
            return 1

        raise NotImplementedError(f"FakeRedisClient.eval does not understand script: {script!r}")

    def _token_bucket(self, args) -> list[int]:
        key = args[0]
        capacity = float(args[1])
        refill_rate = float(args[2])
        now_ms = float(args[3])
        requested = float(args[4])
        ttl_ms = float(args[5])

        self._purge_if_expired(key)
        state = self._hashes.get(key, {})
        tokens = float(state["tokens"]) if "tokens" in state else None
        ts = float(state["ts"]) if "ts" in state else None

        if tokens is None or ts is None:
            tokens = capacity
            ts = now_ms

        elapsed_ms = max(0.0, now_ms - ts)
        tokens = min(capacity, tokens + (elapsed_ms * refill_rate / 1000.0))

        allowed = 0
        retry_after_ms = 0
        if tokens >= requested:
            allowed = 1
            tokens -= requested
        else:
            deficit = requested - tokens
            retry_after_ms = int(math.ceil((deficit / refill_rate) * 1000.0))

        self._hashes[key] = {"tokens": str(tokens), "ts": str(now_ms)}
        self._expiry[key] = self._now() + (ttl_ms / 1000)

        return [allowed, int(math.floor(tokens)), retry_after_ms]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        self.closed = True


class FakeClock:
    """
    Controllable monotonic clock for `FakeRedisClient(clock=FakeClock())`.

    Lets TTL-expiry and lock-expiry tests assert real time-dependent
    behavior instantly, instead of `await asyncio.sleep(31)`.
    """

    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
