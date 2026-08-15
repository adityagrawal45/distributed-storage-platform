"""
Rate-limit response headers (Phase 4 placeholder -> Phase 7 real).

What changed in Phase 7
-----------------------
Phase 4 shipped `RateLimitPlaceholderMiddleware`, an explicit no-op that
stamped `X-RateLimit-Limit: unlimited` on every response purely to reserve
the response contract. That contract is now honored for real: enforcement
happens in `app/core/rate_limiter.py`, invoked per route by the
`rate_limit(...)` dependency in `app/dependencies/rate_limit.py` (see that
module for why a dependency beats middleware for the *decision*).

This middleware keeps exactly one job — reporting. It reads whatever the
dependency stashed on `request.state` and reflects it back as headers, so:

  * routes that opted into a budget report their real remaining budget,
  * routes that did not are still explicitly labelled `unlimited` rather
    than silently omitting the headers, which would leave a client unable
    to distinguish "no limit" from "limit headers not implemented",
  * the header names and semantics are identical to what Phase 4
    published, so no client integrating against NimbusFS needs a breaking
    change now that limits are real — which was the entire point of
    shipping the placeholder in its final position in the chain.

The rejection path (429) does NOT flow through here: it is raised as
`RateLimitExceeded` inside the dependency and rendered by
`app/exceptions/handlers.py::rate_limit_exceeded_exception_handler`, which
attaches `Retry-After` plus the same `X-RateLimit-*` headers to the error
response. Handling headers in two places is deliberate — an exception
short-circuits before the middleware's response object exists.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

UNLIMITED = "unlimited"


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """Reflects the per-route rate-limit decision as `X-RateLimit-*` headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        limit = getattr(request.state, "rate_limit_limit", None)
        remaining = getattr(request.state, "rate_limit_remaining", None)
        category = getattr(request.state, "rate_limit_category", None)

        response.headers.setdefault("X-RateLimit-Limit", str(limit) if limit is not None else UNLIMITED)
        response.headers.setdefault(
            "X-RateLimit-Remaining", str(remaining) if remaining is not None else UNLIMITED
        )
        if category:
            response.headers.setdefault("X-RateLimit-Category", category)

        return response


# Phase 4 name kept as an alias so any external import (or an operator's
# muscle memory) does not break on the rename. It is the same middleware:
# the placeholder's contract was designed to become this.
RateLimitPlaceholderMiddleware = RateLimitHeadersMiddleware
