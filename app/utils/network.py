"""
Reverse-proxy-aware client IP resolution.

Design decision: behind a Google Cloud Load Balancer (or the local
nginx container in docker-compose), `request.client.host` is the
*proxy's* IP, not the real client's — the real client only survives in
`X-Forwarded-For`. But blindly trusting that header lets any caller spoof
their IP by simply sending their own `X-Forwarded-For` header. The fix:
only read the forwarded header when the immediate TCP peer is a
configured, trusted proxy (`settings.TRUSTED_PROXIES`); otherwise trust
only the socket-level peer address, which cannot be spoofed.
"""

from starlette.requests import Request


def get_client_ip(request: Request, trusted_proxies: list[str]) -> str:
    direct_peer = request.client.host if request.client else "unknown"

    if not trusted_proxies:
        return direct_peer

    if "*" not in trusted_proxies and direct_peer not in trusted_proxies:
        return direct_peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return direct_peer

    # X-Forwarded-For is a comma-separated hop chain, left-to-right =
    # original client -> ... -> nearest proxy. The first entry is the
    # original client as long as every hop in between is itself trusted;
    # we keep this simple (single trusted edge, as in docker-compose/GCLB)
    # rather than walking the whole chain.
    return forwarded_for.split(",")[0].strip() or direct_peer


def is_forwarded_https(request: Request, trusted_proxies: list[str]) -> bool:
    """True if a trusted proxy told us the original request was HTTPS."""
    direct_peer = request.client.host if request.client else "unknown"
    if not trusted_proxies or ("*" not in trusted_proxies and direct_peer not in trusted_proxies):
        return request.url.scheme == "https"
    forwarded_proto = request.headers.get("x-forwarded-proto")
    return (forwarded_proto or request.url.scheme).lower() == "https"
