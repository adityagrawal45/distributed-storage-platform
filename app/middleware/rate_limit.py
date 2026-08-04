"""
Rate limiting — PLACEHOLDER (see README Phase 4 scope).

Design decisions:
- Disabled by default (`RATE_LIMIT_ENABLED=false`). Phase 4's brief is to
  prepare the infrastructure, not to pick real production limits/policy
  (per-user vs per-IP vs per-endpoint, burst allowances, 429 backoff
  hints) — that's a deliberate decision for a later phase, made with
  real traffic data in hand.
- What IS real here: a working fixed-window counter in Redis
  (`CacheService.increment`, atomic `INCR` + `EXPIRE`-once-per-window),
  keyed by the trusted-proxy-aware client IP (see
  `app/utils/network.py`) and the current UTC minute. Flipping
  `RATE_LIMIT_ENABLED=true` today already enforces
  `RATE_LIMIT_REQUESTS_PER_MINUTE` correctly across every instance in the
  fleet, because the counter lives in shared Redis, not per-instance
  memory — a naive in-memory counter would let a client get N requests
  PER INSTANCE instead of N total, which silently breaks the moment
  there's more than one replica.
- Fails OPEN if Redis is unavailable: a rate limiter's job is to protect
  the backend from abuse, not to become a second point of failure that
  blocks all traffic when its own dependency degrades.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.logging.logger import get_logger
from app.schemas.response import APIResponse
from app.services.cache_service import CacheService
from app.utils.network import get_client_ip

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, cache_service: CacheService | None = None):
        super().__init__(app)
        self._cache = cache_service or CacheService()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        client_ip = get_client_ip(request, settings.TRUSTED_PROXIES)
        window_key = f"ratelimit:{client_ip}"

        count = await self._cache.increment(window_key, ttl_seconds=60)
        if count is None:
            # Redis unavailable -> fail open, see module docstring.
            return await call_next(request)

        if count > settings.RATE_LIMIT_REQUESTS_PER_MINUTE:
            logger.warning("rate_limit_exceeded", client_ip=client_ip, count=count)
            body = APIResponse(success=False, message="Rate limit exceeded. Please slow down.").model_dump(
                mode="json"
            )
            return JSONResponse(status_code=429, content=body, headers={"Retry-After": "60"})

        return await call_next(request)
