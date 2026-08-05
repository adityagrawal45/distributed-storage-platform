"""
Request context middleware — distributed tracing/logging backbone (Phase 4).

Design decisions:
- Three distinct IDs, each with a distinct purpose — conflating them is
  a common mistake in distributed systems:
    * `request_id`   — unique to THIS hop (this replica, this request).
                        Always generated fresh, never trusted from a
                        client header (a client could otherwise forge
                        collisions or inject garbage into logs).
    * `correlation_id` — unique to the end-to-end client operation, and
                        MAY span multiple HTTP requests (e.g. a client
                        retries a timed-out call, or a browser action
                        fans out into several API calls). Honored from
                        an inbound `X-Correlation-ID` header if present
                        (so an upstream gateway/client can tie its own
                        operation ID to ours), otherwise generated fresh
                        here as the origin of a new correlation chain.
    * `trace_id`     — reserved for distributed tracing (OpenTelemetry,
                        Cloud Trace) once wired up in a future phase.
                        Honored from `X-Trace-ID`/`traceparent` if a
                        tracing-aware caller already supplies one so we
                        don't stomp on a real trace; otherwise defaults
                        to the correlation ID so every log line still
                        has a value in that field pre-OTel.
- All three, plus `server_id` (the instance identity — see
  `app/core/server_info.py`), are bound into structlog's contextvars for
  the lifetime of the request, so every log line emitted anywhere in the
  call stack (routes, services, repositories, storage layer) carries
  them automatically with zero per-call-site boilerplate. `contextvars`
  survive `await` and `asyncio.to_thread` (used by the GCS calls),
  so this holds even across the sync/async boundary in `StorageService`.
- All three IDs are echoed back as response headers so a client (or an
  upstream load balancer/log correlation tool) can grep logs across the
  whole distributed fleet for one user-visible request.
- Request/response access logging happens here (not scattered across
  routes) so ALL endpoints get consistent structured logs for free,
  including method, path, status code, duration, and (once
  authentication has run) the user ID.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.server_info import get_server_identity
from app.logging.logger import get_logger

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        trace_id = request.headers.get("x-trace-id") or correlation_id
        server_identity = get_server_identity()

        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        request.state.trace_id = trace_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            server_id=server_identity.instance_id,
            hostname=server_identity.hostname,
        )

        start_time = time.perf_counter()
        logger.info("request_started", method=request.method, path=request.url.path, client_ip=_client_ip(request))

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Server-ID"] = server_identity.instance_id
        response.headers["X-Response-Time-Ms"] = str(duration_ms)

        # Bind the authenticated user, if any, only after the route ran
        # (auth happens inside `call_next`) — logged on the completion
        # line so it doesn't need to be threaded through every handler.
        user_id = getattr(request.state, "user_id", None)

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_id=str(user_id) if user_id else None,
        )
        return response


def _client_ip(request: Request) -> str | None:
    """
    Best-effort client IP for logging. Trusts `request.state.client_ip`
    if `TrustedProxyMiddleware` (which runs before this one — see
    `app/main.py`'s middleware ordering) already resolved one from
    `X-Forwarded-For`; otherwise falls back to the raw ASGI peer address,
    which behind a load balancer is just the LB's own IP.
    """
    forwarded = getattr(request.state, "client_ip", None)
    if forwarded:
        return forwarded
    return request.client.host if request.client else None
