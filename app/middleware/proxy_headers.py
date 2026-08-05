"""
Trusted-proxy / forwarded-headers middleware (Phase 4).

Design decisions:
- Behind Google Cloud Load Balancer (this phase's target architecture),
  the ASGI server sees the LB as the TCP peer, not the real client — the
  real client IP and original scheme (`https`) only survive in
  `X-Forwarded-For` / `X-Forwarded-Proto` headers. Trusting those headers
  blindly is a spoofing vector for any client that can reach the app
  directly, so they are only honored when `Settings.TRUSTED_PROXIES`
  says so (`"*"` for this phase's "just get the plumbing right" scope;
  pin to real proxy CIDRs once the network topology is locked down —
  see `Settings.TRUSTED_PROXIES_RAW` docstring).
- `X-Forwarded-For` is a comma-separated hop chain (each proxy appends
  its predecessor's address); the *first* entry is the original client,
  everything after it is intermediate proxies. We take the first entry.
- Populates `request.state.client_ip` / `request.state.scheme` /
  `request.state.is_secure` for downstream code (logging, security
  headers, HTTPS-only enforcement) rather than mutating `request.scope`
  directly — mutating ASGI scope out from under Starlette mid-request is
  fragile; reading resolved values off `request.state` is not.
- Runs before `RequestContextMiddleware` (added first / outermost — see
  `app/main.py`'s middleware ordering) so the resolved client IP is
  available when request-started is logged.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings

settings = get_settings()


class TrustedProxyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        peer_ip = request.client.host if request.client else None
        trusted = settings.trust_any_proxy or peer_ip in settings.TRUSTED_PROXIES

        client_ip = peer_ip
        scheme = request.url.scheme

        if trusted:
            forwarded_for = request.headers.get("x-forwarded-for")
            if forwarded_for:
                client_ip = forwarded_for.split(",")[0].strip()

            forwarded_proto = request.headers.get("x-forwarded-proto")
            if forwarded_proto:
                scheme = forwarded_proto.strip().lower()

        request.state.client_ip = client_ip
        request.state.scheme = scheme
        request.state.is_secure = scheme == "https"

        return await call_next(request)
