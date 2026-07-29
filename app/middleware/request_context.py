"""
Request context middleware.

Design decisions:
- A unique `request_id` (UUID4) is generated per request, stored on
  `request.state`, echoed back in the `X-Request-ID` response header,
  and bound into structlog's contextvars. Every log line emitted while
  handling this request automatically includes it — critical for tracing
  a single request's journey through logs once there are multiple pods.
- Request/response logging happens here (not scattered across routes)
  so ALL endpoints get consistent access logs for free, including
  method, path, status code, and duration.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging.logger import get_logger

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start_time = time.perf_counter()
        logger.info("request_started", method=request.method, path=request.url.path)

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
