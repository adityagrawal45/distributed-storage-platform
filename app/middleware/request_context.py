"""
Request context middleware.

Design decisions:
- Three distinct IDs, each with a different job:
    * `request_id`  - unique to THIS hop (this instance, this call).
      Always freshly generated, never trusted from a client.
    * `correlation_id` - unique to the CLIENT-VISIBLE operation. If the
      caller sends `X-Correlation-ID` (e.g. a frontend generating one per
      user action, or another one of our own services calling in), we
      echo it back unchanged; otherwise it defaults to this request's own
      `request_id`. This is what lets a support engineer grep logs across
      a retry (client resends the same correlation ID) or across a
      multi-call user flow.
    * `trace_id` - a 32-hex-char identifier in the same shape as an
      OpenTelemetry trace ID (`X-Trace-ID` in, or generated). Kept
      separate from `correlation_id` on purpose: once real OpenTelemetry
      instrumentation lands in a later phase, `trace_id` is the field
      that gets replaced by the SDK's real trace context — decoupling it
      now means that swap won't touch `correlation_id`'s semantics.
  All three are bound into structlog's contextvars, so EVERY log line
  emitted anywhere during this request — middleware, dependency,
  service, repository, storage layer — carries them automatically. This
  is deliberate: we do not thread a `request_id` parameter through every
  function signature (that would violate DRY and couple every layer to
  tracing concerns); contextvars propagate through the async call stack
  for free, which is exactly what "Request Tracing" needs without
  "Request Tracing" leaking into every service's method signature.
- `X-Server-ID` echoes this process's identity (see
  `app/core/server_identity.py`) so a client-reported bug ("it happened
  again just now") can be matched to one instance's logs in a fleet of N.
- `X-Response-Time-Ms` is the median way ops dashboards graph latency per
  endpoint without needing a separate APM agent wired in yet.
- `app.state.active_requests` is incremented/decremented around the
  whole handler. `app.main`'s graceful shutdown polls this counter so a
  SIGTERM'd instance can finish in-flight requests before its DB/Redis
  pools are torn out from under them (see README "Shutdown Lifecycle").
- Client IP resolution goes through `get_client_ip` (trusted-proxy-aware)
  rather than trusting `request.client.host` or `X-Forwarded-For`
  unconditionally — see `app/utils/network.py`.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.server_identity import SERVER_IDENTITY
from app.logging.logger import get_logger
from app.utils.network import get_client_ip

logger = get_logger(__name__)
settings = get_settings()


def _generate_trace_id() -> str:
    """32 lowercase hex chars — same shape as a W3C traceparent trace-id, for painless OTel adoption later."""
    return uuid.uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        correlation_id = request.headers.get("x-correlation-id") or request_id
        trace_id = request.headers.get("x-trace-id") or _generate_trace_id()
        client_ip = get_client_ip(request, settings.TRUSTED_PROXIES)

        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        request.state.trace_id = trace_id
        request.state.client_ip = client_ip

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            server_id=SERVER_IDENTITY.instance_id,
        )

        app_state = request.app.state
        app_state.active_requests = getattr(app_state, "active_requests", 0) + 1

        start_time = time.perf_counter()
        logger.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            client_ip=client_ip,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.exception(
                "request_failed", method=request.method, path=request.url.path, duration_ms=duration_ms
            )
            raise
        finally:
            app_state.active_requests -= 1

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Server-ID"] = SERVER_IDENTITY.instance_id
        response.headers["X-Response-Time-Ms"] = str(duration_ms)

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
