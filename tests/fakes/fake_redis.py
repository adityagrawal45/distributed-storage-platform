"""
In-memory fake of the `redis.asyncio.Redis` surface actually used by
Phase 4 code (`DistributedLock`, `IdempotencyService`,
`check_redis_connection`). Mirrors `tests/fakes/fake_gcs.py`'s
philosophy: a hand-written fake that really stores/expires values, not
a `Mock`, so tests assert on real behavior (NX fails once a key
exists; a Lua-checked release only deletes a matching token) rather
than "was this method called".

Deliberately implements only what NimbusFS calls — this is not a
general Redis emulator.
"""

from __future__ import annotations

import fnmatch
import time


class FakeRedisClient:
    def __init__(self):
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self.closed = False

    def _is_expired(self, key: str) -> bool:
        expires_at = self._expiry.get(key)
        return expires_at is not None and time.monotonic() >= expires_at

    def _purge_if_expired(self, key: str) -> None:
        if key in self._store and self._is_expired(key):
            del self._store[key]
            self._expiry.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def set(
        self,
        name: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool:
        self._purge_if_expired(name)

        if nx and name in self._store:
            return False

        self._store[name] = value
        if ex is not None:
            self._expiry[name] = time.monotonic() + ex
        elif px is not None:
            self._expiry[name] = time.monotonic() + (px / 1000)
        else:
            self._expiry.pop(name, None)
        return True

    async def get(self, name: str) -> str | None:
        self._purge_if_expired(name)
        return self._store.get(name)

    async def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            self._purge_if_expired(name)
            if name in self._store:
                del self._store[name]
                self._expiry.pop(name, None)
                count += 1
        return count

    async def keys(self, pattern: str = "*") -> list[str]:
        return [k for k in list(self._store.keys()) if not self._is_expired(k) and fnmatch.fnmatch(k, pattern)]

    async def eval(self, script: str, numkeys: int, *keys_and_args) -> int:
        """
        Only understands the two scripts actually used by
        `app/core/distributed_lock.py` (release-if-token-matches,
        pexpire-if-token-matches) — pattern-matches on script content
        rather than implementing a Lua interpreter.
        """
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
            self._expiry[key] = time.monotonic() + (ttl_ms / 1000)
            return 1

        raise NotImplementedError(f"FakeRedisClient.eval does not understand script: {script!r}")

    async def aclose(self) -> None:
        self.closed = True
