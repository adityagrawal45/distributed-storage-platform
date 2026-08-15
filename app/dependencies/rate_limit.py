"""
Rate-limiting as a FastAPI dependency (Phase 7).

Why a dependency and not middleware
-----------------------------------
Middleware sees only the raw ASGI request — a method and a path string.
Deciding "is this the login route or the search route" there means
maintaining a path-pattern table that silently rots the moment a route is
renamed or a path parameter is added. A dependency is declared *on the
route itself*:

    dependencies=[Depends(rate_limit(RateLimitCategory.SEARCH))]

so the budget lives next to the endpoint it governs, moves with it, is
visible in the OpenAPI schema, and cannot drift. It also runs before the
handler body (so an over-limit request costs no DB or GCS work) and can be
overridden per-route in tests like any other dependency.

Identity resolution, and why the token is decoded here
------------------------------------------------------
Limits must be per-caller. The natural key is the authenticated user ID,
but `get_current_user` is a *different* dependency, and FastAPI does not
guarantee that it resolves before this one — relying on
`request.state.user_id` being populated would be an ordering assumption
that works until it does not.

So this dependency extracts the identity itself: it decodes the Bearer
token's `sub` claim locally (signature-verified, no database round trip —
`decode_token` is pure CPU) and falls back to the client IP resolved by
`TrustedProxyMiddleware` when there is no valid token. Consequences worth
stating plainly:

- An invalid/expired token is limited by IP, which is correct: an attacker
  brute-forcing tokens should be throttled, and they have no identity.
- Two users behind one NAT share an IP bucket on unauthenticated routes
  (login/register). That is the intended behavior for credential-stuffing
  defense, and the reason those categories get a deliberately generous
  budget relative to a single human's usage.
- Authorization is NOT performed here. The token is used only as an
  identity hint for bucketing; every route still runs its own
  `CurrentUser` dependency and its own ownership checks. A forged
  identity buys an attacker a *stricter* or *different* bucket, never
  access to anything.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import redis.asyncio as redis
from fastapi import Depends, Request

from app.core.cache.keys import CacheKeyBuilder
from app.core.config import get_settings
from app.core.rate_limiter import RateLimitCategory, RateLimiter
from app.core.security.tokens import TokenType, decode_token
from app.database.redis import get_redis
from app.exceptions.custom_exceptions import RateLimitExceeded


def get_rate_limiter(client: Annotated[redis.Redis, Depends(get_redis)]) -> RateLimiter:
    """
    DI provider for the limiter.

    Lives in this module rather than `app/dependencies/providers.py` to
    keep the import graph acyclic (`providers` imports this module and
    re-exports `RateLimiterDep`, so the wiring is still discoverable from
    the usual place). It depends only on `get_redis`, which tests already
    override with `FakeRedisClient` — so rate limiting is exercised by the
    suite with no real Redis, exactly like locks and idempotency.
    """
    return RateLimiter(client, get_settings(), CacheKeyBuilder(get_settings().CACHE_KEY_PREFIX))


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def _resolve_identity(request: Request) -> tuple[str, str]:
    """Returns `(identity, identity_type)` — user ID if provable, else client IP."""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        try:
            payload = decode_token(token, expected_type=TokenType.ACCESS)
            subject = payload.get("sub")
            if subject:
                return f"user:{subject}", "user"
        except Exception:
            # Not a usable token — fall through to IP. Deliberately not
            # logged: an expired token on a normal request is routine, and
            # logging it here would drown the signal in noise.
            pass

    client_ip = getattr(request.state, "client_ip", None)
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"
    return f"ip:{client_ip}", "ip"


def rate_limit(
    category: RateLimitCategory,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    Dependency factory: returns a dependency enforcing `category`'s budget.

    On rejection it raises `RateLimitExceeded`, which
    `rate_limit_exceeded_exception_handler` renders as a 429 in the
    standard `APIResponse` envelope with `Retry-After` and `X-RateLimit-*`
    headers. Raising (rather than returning a Response here) keeps the
    error shape identical to every other error the API produces.
    """

    async def _dependency(
        request: Request,
        limiter: RateLimiterDep,
    ) -> None:
        if not limiter.enabled:
            return

        identity, identity_type = _resolve_identity(request)
        result = await limiter.check(category, identity, identity_type=identity_type)

        # Stashed for RateLimitHeadersMiddleware, which attaches the
        # X-RateLimit-* headers to the *successful* response. The
        # rejection path carries its own headers via the exception handler.
        request.state.rate_limit_limit = result.limit
        request.state.rate_limit_remaining = result.remaining
        request.state.rate_limit_category = result.category.value

        if not result.allowed:
            raise RateLimitExceeded(
                detail=(
                    f"Rate limit exceeded for '{result.category.value}': "
                    f"{result.limit} requests per {limiter.rule_for(category).window_seconds}s."
                ),
                retry_after_seconds=result.retry_after_seconds,
                limit=result.limit,
                remaining=result.remaining,
                category=result.category.value,
            )

    return _dependency
