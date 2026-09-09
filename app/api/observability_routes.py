"""
`GET /metrics` — Prometheus text-exposition endpoint (Phase 11).

Deliberately mounted at the bare root path, NOT under `settings.API_V1_PREFIX`
like every versioned business endpoint (`app/api/v1/router.py`). `/metrics`
is an infrastructure/scrape contract, not a versioned API a client
integrates against — Google Managed Prometheus's `PodMonitoring` CRD
(`k8s/24-podmonitoring.yaml`) and any ad-hoc `curl` both expect the
conventional unversioned path.

No authentication. This matches how every Prometheus-style scrape
endpoint is normally deployed (Redis exporter, node exporter, kube-state-
metrics, etc.): access control is a network-layer concern
(`k8s/11-networkpolicy.yaml` restricts *ingress* to the Pod at all;
scraping is additionally same-namespace/cluster-internal only, since this
Service has no public Ingress path to `/metrics` — see
`k8s/15-ingress.yaml`, which only routes `/api/v1/*` and health checks
externally). No secret, credential, or per-user data is ever exposed
here — only the bounded-cardinality counters/histograms/gauges defined
in `app/core/metrics.py`.
"""

from collections.abc import Callable

from fastapi import APIRouter, Response

from app.core import metrics as app_metrics
from app.database.session import engine

router = APIRouter(tags=["Observability"])


def _set_pool_gauge(state: str, getter: Callable[[], int]) -> None:
    """
    One gauge update, as its own named function rather than a lambda
    (Phase 12 mypy pass): a lambda closing over a `for` loop's own
    variables needs the `lambda x=x: ...` default-argument trick to
    avoid late-binding to the loop's FINAL value — mypy cannot infer
    that trick's parameter types from a `getattr(..., None)`-typed
    tuple, and the trick itself is exactly the kind of "clever" code
    this codebase otherwise avoids. A plain function called once per
    loop iteration with `state`/`getter` as ordinary arguments has
    neither problem.
    """
    app_metrics.safe_call(
        lambda: app_metrics.DB_POOL_CONNECTIONS.labels(state=state).set(getter()),
        operation="db_pool_gauge_update",
    )


def _update_pool_gauges() -> None:
    """
    Connection-pool gauges are refreshed at scrape time rather than
    pushed on every checkout/checkin — cheaper, and a gauge is allowed to
    be "as of last scrape" without losing meaning (unlike a counter).
    `engine.pool` only exposes these introspection methods for
    `QueuePool`-family pools; guarded because the test suite's SQLite
    engine does not use one.
    """
    pool = engine.pool
    for state, getter in (
        ("checked_out", getattr(pool, "checkedout", None)),
        ("checked_in", getattr(pool, "checkedin", None)),
        ("overflow", getattr(pool, "overflow", None)),
    ):
        if getter is None:
            continue
        _set_pool_gauge(state, getter)


@router.get(
    "/metrics",
    summary="Prometheus-format application metrics (scraped by Google Managed Prometheus)",
    include_in_schema=False,
)
async def metrics_endpoint() -> Response:
    _update_pool_gauges()
    body, content_type = app_metrics.render()
    return Response(content=body, media_type=content_type)
