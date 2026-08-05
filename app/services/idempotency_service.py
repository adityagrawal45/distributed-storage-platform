"""
Idempotency-Key support, backed by Redis (Phase 4).

Design decisions:
- Purpose: make non-idempotent-by-nature endpoints (chiefly
  `POST /files/upload`) safe to retry — over a flaky mobile network, a
  client that times out waiting for a response has no way to know
  whether the upload succeeded server-side or not, and a naive retry
  would create a duplicate file. RFC-style `Idempotency-Key` header
  support fixes this: the client generates one UUID per *logical*
  operation and sends it on every retry attempt of that same operation.
- Storage: Redis, not Postgres — this is exactly the kind of short-
  lived (`IDEMPOTENCY_KEY_TTL_SECONDS`, default 24h), high-write,
  non-relational state Redis is for for, and it's already a shared
  dependency every replica has, which is required for this to work
  correctly across a fleet (two different replicas must see the same
  in-flight/completed state for the same key).
- Concurrency: the *first* write for a given key uses `SET NX` — an
  atomic "claim or fail" — so if two requests carrying the same key
  race in from two different replicas, exactly one wins the claim and
  proceeds; the other is told to back off (`IdempotencyKeyInProgressException`,
  mapped to `409 Conflict`) rather than both executing the upload.
- Replay safety: a *fingerprint* of the logically-relevant request
  parameters (not full file bytes — see `compute_fingerprint`'s
  docstring for the trade-off) is stored alongside the cached response.
  A later request reusing the same key is only ever replayed if the
  fingerprint matches; a mismatched fingerprint means the client reused
  a key for a *different* logical operation, which is a client bug
  surfaced as `422` rather than silently returning the wrong cached
  response.
- Failure handling: `fail()` deletes the in-progress marker so a request
  that genuinely failed (e.g. upstream GCS outage) can be retried
  immediately with the same key, instead of the client being stuck
  behind a 24h TTL for an operation that never actually completed.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as redis

from app.exceptions.custom_exceptions import IdempotencyKeyInProgressException, IdempotencyKeyReplayedException
from app.logging.logger import get_logger

logger = get_logger(__name__)


class IdempotencyOutcome(str, Enum):
    PROCEED = "proceed"  # no prior record — caller should execute the operation
    REPLAY = "replay"  # identical prior request already completed — return its cached response


@dataclass
class IdempotencyCheckResult:
    outcome: IdempotencyOutcome
    cached_status_code: int | None = None
    cached_body: dict | None = None


def compute_fingerprint(*parts: str) -> str:
    """
    Fingerprints the logically-relevant, cheaply-reproducible parts of a
    request (user ID, route, filename, folder, declared size — see call
    site) rather than hashing the full request body.

    Trade-off, stated plainly: this detects "same key reused for an
    obviously different operation" (different file, different folder),
    but does NOT cryptographically guarantee byte-for-byte identical
    payloads — doing that for a streamed multipart upload would require
    buffering the entire file before starting the real upload, which
    defeats the point of streaming uploads. Exact-content duplicate
    detection already exists independently, via SHA-256 content hashing
    in `FileUploadService` (Phase 3) — idempotency and content-dedup are
    complementary, not the same mechanism.
    """
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, client: redis.Redis, ttl_seconds: int):
        self._client = client
        self._ttl_seconds = ttl_seconds

    def _redis_key(self, user_id: uuid.UUID, idempotency_key: str) -> str:
        return f"idempotency:{user_id}:{idempotency_key}"

    async def check_or_begin(
        self, user_id: uuid.UUID, idempotency_key: str, fingerprint: str
    ) -> IdempotencyCheckResult:
        """
        Atomically claims the key for a new attempt, or returns the
        outcome for an existing one. Must be called before the
        non-idempotent operation executes.
        """
        key = self._redis_key(user_id, idempotency_key)
        record = {"status": "in_progress", "fingerprint": fingerprint}

        claimed = await self._client.set(key, json.dumps(record), nx=True, ex=self._ttl_seconds)
        if claimed:
            return IdempotencyCheckResult(outcome=IdempotencyOutcome.PROCEED)

        raw = await self._client.get(key)
        if raw is None:
            # Lost a race between our failed SET NX and a concurrent
            # fail()/expiry — safe to just proceed as a fresh attempt.
            return IdempotencyCheckResult(outcome=IdempotencyOutcome.PROCEED)

        existing = json.loads(raw)

        if existing.get("fingerprint") != fingerprint:
            raise IdempotencyKeyReplayedException()

        if existing.get("status") == "in_progress":
            raise IdempotencyKeyInProgressException()

        logger.info("idempotency_key_replayed", idempotency_key=idempotency_key, user_id=str(user_id))
        return IdempotencyCheckResult(
            outcome=IdempotencyOutcome.REPLAY,
            cached_status_code=existing.get("status_code"),
            cached_body=existing.get("body"),
        )

    async def complete(
        self, user_id: uuid.UUID, idempotency_key: str, fingerprint: str, status_code: int, body: dict
    ) -> None:
        """Persists the successful outcome so retries replay it instead of re-executing."""
        key = self._redis_key(user_id, idempotency_key)
        record = {
            "status": "completed",
            "fingerprint": fingerprint,
            "status_code": status_code,
            "body": body,
        }
        await self._client.set(key, json.dumps(record), ex=self._ttl_seconds)

    async def fail(self, user_id: uuid.UUID, idempotency_key: str) -> None:
        """Releases the in-progress claim so the same key can be retried immediately."""
        key = self._redis_key(user_id, idempotency_key)
        await self._client.delete(key)
