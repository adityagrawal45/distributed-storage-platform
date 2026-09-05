"""
`MetricsMiddleware` — HTTP RED metrics (Phase 11).

A separate middleware from `RequestContextMiddleware` on purpose, matching
this codebase's existing one-concern-per-middleware convention (compare
`SecurityHeadersMiddleware` / `RateLimitHeadersMiddleware` /
`TrustedProxyMiddleware`, each doing exactly one thing). `RequestContextMiddleware`
owns identity/logging; this one owns counting.

Route *templates*, not raw paths
---------------------------------
`request.url.path` for `GET /api/v1/files/3fa8...-uuid` is a different
string per file — using it as a metric label would create one time
series per file ID forever (see `app/core/metrics.py`'s cardinality
docstring). Starlette resolves the matched route (with its `{file_id}`-
shaped template) onto `request.scope["route"]` before the endpoint runs,
so reading `request.scope["route"].path` AFTER `call_next` returns gives
the bounded template string instead. A request that matches no route at
all (404) has no `route` in scope; those are labeled `"unmatched"` so a
flood of probing/scanning traffic still shows up as one bounded series,
not as an unbounded one keyed on whatever path was requested.
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_PROGRESS,
    HTTP_REQUESTS_TOTAL,
    safe_call,
)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        safe_call(lambda: HTTP_REQUESTS_IN_PROGRESS.labels(method=method).inc(), operation="http_in_progress_inc")
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            route = _route_template(request)
            safe_call(
                lambda: HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route).observe(duration),
                operation="http_duration_observe",
            )
            safe_call(
                lambda: HTTP_REQUESTS_TOTAL.labels(method=method, route=route, status_code="500").inc(),
                operation="http_requests_total_inc",
            )
            raise
        finally:
            safe_call(
                lambda: HTTP_REQUESTS_IN_PROGRESS.labels(method=method).dec(), operation="http_in_progress_dec"
            )

        duration = time.perf_counter() - started
        route = _route_template(request)
        status_code = str(response.status_code)
        safe_call(
            lambda: HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route).observe(duration),
            operation="http_duration_observe",
        )
        safe_call(
            lambda: HTTP_REQUESTS_TOTAL.labels(method=method, route=route, status_code=status_code).inc(),
            operation="http_requests_total_inc",
        )
        return response
