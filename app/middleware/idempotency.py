"""
Idempotency-Key middleware.

Design decisions:
- Global, opt-in, header-driven: ANY mutating endpoint (`POST`/`PUT`/
  `PATCH`/`DELETE`) automatically supports idempotency the moment a
  client sends an `Idempotency-Key` header — no per-route wiring needed.
  This matters because "prevent duplicate uploads" (README) and "prevent
  duplicate folder/metadata creation" are the same underlying problem:
  a client's network retry (timeout, connection reset) re-sends a
  request whose first attempt actually succeeded server-side. A single,
  route-agnostic middleware solves it everywhere at once (DRY) instead
  of a bespoke duplicate-check per service.
- Three-outcome flow, keyed by `sha256(auth_header:method:path:idempotency_key)`:
    1. Key not seen before -> claim an "in-progress" marker (`SET NX`),
       run the handler, cache the final response.
    2. Key maps to a COMPLETED response already -> replay that exact
       response (status, body, `Content-Type`) without re-running the
       handler at all. Tagged `X-Idempotent-Replay: true` so clients/logs
       can tell a replay from a fresh execution.
    3. Key maps to an IN-PROGRESS marker (another request with the same
       key is still executing, e.g. two near-simultaneous retries) ->
       `409 Conflict` immediately. This is a deliberate fail-CLOSED
       choice, unlike the cache layer: silently letting both through
       would defeat the entire point of idempotency (this is exactly the
       "prevent duplicate uploads" case — two racing retries must not
       both create a file).
- If Redis itself is unavailable, this middleware fails OPEN (lets the
  request through unguarded) rather than blocking all writes on a cache
  outage — an idempotency guarantee is a nice-to-have safety net, not a
  transaction boundary. Contrast with the in-progress marker's fail
  CLOSED for the "two concurrent replays" case above: that failure mode
  is about protecting against duplicate execution *when Redis is up*, not
  about Redis's own availability.
- Only success/client-error responses (`status_code < 500`) are cached.
  A `5xx` means the operation likely didn't actually complete, so the
  next retry with the same key should be allowed to actually try again
  rather than replaying a failure forever.
- `BaseHTTPMiddleware` fully buffers the downstream response's body
  anyway (a documented Starlette characteristic), so capturing it here to
  cache is not adding a new performance cost over what the framework
  already does per request.
"""

import hashlib
import json

import redis.asyncio as redis_asyncio
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.database import redis as redis_db
from app.logging.logger import get_logger
from app.schemas.response import APIResponse

logger = get_logger(__name__)

IDEMPOTENT_GUARDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_HEADER = "idempotency-key"


def _fingerprint(request: Request, idempotency_key: str) -> str:
    # Scoped by the caller's bearer token (not a resolved user id — this
    # middleware runs before auth dependencies resolve) so two different
    # users sending the same client-chosen key never collide.
    auth = request.headers.get("authorization", "")
    raw = f"{auth}:{request.method}:{request.url.path}:{idempotency_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        idempotency_key = request.headers.get(_HEADER)

        if (
            not settings.IDEMPOTENCY_ENABLED
            or request.method not in IDEMPOTENT_GUARDED_METHODS
            or not idempotency_key
        ):
            return await call_next(request)

        fingerprint = _fingerprint(request, idempotency_key)
        result_key = f"idempotency:result:{fingerprint}"
        progress_key = f"idempotency:in-progress:{fingerprint}"

        try:
            cached = await self._get(result_key)
        except redis_asyncio.RedisError as exc:
            logger.warning("idempotency_lookup_failed_open", error=str(exc))
            return await call_next(request)

        if cached is not None:
            payload = json.loads(cached)
            response = Response(
                content=payload["body"],
                status_code=payload["status_code"],
                media_type=payload.get("media_type"),
            )
            for name, value in payload.get("headers", {}).items():
                response.headers[name] = value
            response.headers["X-Idempotent-Replay"] = "true"
            return response

        try:
            claimed = await self._claim(progress_key, settings.IDEMPOTENCY_KEY_TTL_SECONDS)
        except redis_asyncio.RedisError as exc:
            logger.warning("idempotency_claim_failed_open", error=str(exc))
            claimed = True

        if not claimed:
            body = APIResponse(
                success=False,
                message="A request with this Idempotency-Key is already being processed.",
            ).model_dump(mode="json")
            return JSONResponse(status_code=409, content=body)

        response = await call_next(request)

        if response.status_code < 500:
            body_bytes = b"".join([chunk async for chunk in response.body_iterator])
            payload = {
                "status_code": response.status_code,
                "media_type": response.media_type,
                "headers": {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() in {"content-type", "content-disposition"}
                },
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
            try:
                await self._store(result_key, json.dumps(payload), settings.IDEMPOTENCY_KEY_TTL_SECONDS)
            except redis_asyncio.RedisError as exc:
                logger.warning("idempotency_store_failed", error=str(exc))

            return Response(
                content=body_bytes,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=dict(response.headers),
            )

        return response

    @staticmethod
    async def _get(key: str) -> str | None:
        client = redis_db.get_redis_client()
        try:
            return await client.get(key)
        finally:
            await client.aclose()

    @staticmethod
    async def _claim(key: str, ttl_seconds: int) -> bool:
        client = redis_db.get_redis_client()
        try:
            return bool(await client.set(key, "1", nx=True, ex=ttl_seconds))
        finally:
            await client.aclose()

    @staticmethod
    async def _store(key: str, value: str, ttl_seconds: int) -> None:
        client = redis_db.get_redis_client()
        try:
            await client.set(key, value, ex=ttl_seconds)
        finally:
            await client.aclose()
