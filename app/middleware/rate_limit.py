"""
Rate limiting placeholder (Phase 4).

Design decisions:
- Explicitly a NO-OP placeholder, not a real limiter — actual rate
  limiting (token bucket / sliding window, backed by the shared Redis
  every replica already has, keyed by user ID or client IP) is
  deliberately deferred to a future phase per this phase's scope.
- Wired into the middleware stack now, in its final position in the
  chain, so:
    1. The request/response contract it will eventually need (a
       `429 Too Many Requests` short-circuit, `Retry-After` header) is
       decided and documented here before it's implemented, not bolted
       on later wherever happens to be convenient.
    2. Every response already carries an `X-RateLimit-*` header
       placeholder shape, so API consumers integrating against NimbusFS
       today won't need a breaking client change when real limits land.
- Reads `request.state.client_ip` (populated by `TrustedProxyMiddleware`,
  which must run before this one) as the future rate-limit key, proving
  out that the two middlewares compose correctly even though this one
  doesn't act on it yet.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RateLimitPlaceholderMiddleware(BaseHTTPMiddleware):
    """
    No-op today. Documents the seam a real limiter will occupy:
    - Would reject over-limit requests with 429 + `Retry-After` before
      `call_next` runs (fail fast, don't waste a DB/GCS round trip).
    - Would decrement/check a Redis-backed counter keyed on
      `request.state.client_ip` (unauthenticated) or `current_user.id`
      (authenticated), scoped per-route-group so `/files/upload` and
      `/auth/login` get independent budgets.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        # Placeholder values only — no counting happens yet. Real limits
        # will replace these with the actual budget/remaining/reset.
        response.headers.setdefault("X-RateLimit-Limit", "unlimited")
        response.headers.setdefault("X-RateLimit-Remaining", "unlimited")
        return response
