"""
Application metrics (Phase 11).

Why `prometheus_client` and not a full Prometheus/Grafana deployment
---------------------------------------------------------------------
This does NOT stand up a Prometheus server, Alertmanager, or Grafana.
`prometheus_client` is a small, dependency-free instrumentation library:
it keeps counters/histograms/gauges in-process and renders them as text
on demand. NimbusFS runs on GKE, which ships **Google Managed Prometheus
(GMP)** as a built-in cluster add-on — a `PodMonitoring` resource (see
`k8s/24-podmonitoring.yaml`) tells GMP's already-running collector to
scrape this process's `/metrics` endpoint on an interval and ship the
samples into Cloud Monitoring, where they sit next to GKE/Cloud SQL/
Memorystore's own native metrics and can back Cloud Monitoring alert
policies and dashboards. That means: zero additional infrastructure to
operate (no Prometheus server, no persistent volume, no Grafana
deployment, nothing new to patch or scale), while still exposing metrics
in the one exposition format every scraper (GMP, a laptop's `curl`, a
future self-hosted Prometheus) understands. See `docs/monitoring.md`
("Google Cloud Monitoring vs Prometheus/Grafana") for the full
comparison this choice is based on — the two are not mutually exclusive
here; GMP *is* a managed Prometheus, which is exactly why no separate
Prometheus/Grafana deployment is justified.

Cardinality discipline
-----------------------
Every label below is drawn from a small, closed, code-controlled set
(HTTP method, a route *template* like `/api/v1/files/{file_id}`, a
status code, a cache/worker/rate-limit *category* name). Nothing here is
keyed on `user_id`, `file_id`, `request_id`, `trace_id`, or any other
unbounded value — those belong in logs and span records
(`app/core/tracing.py`), which is exactly where NimbusFS puts them. A
counter labeled by `user_id` would create one new time series PER USER
FOREVER; Prometheus/GMP hold every distinct label combination in memory
indefinitely (until the series goes stale), so an unbounded label set is
a slow, silent memory/cost leak that gets discovered in a monitoring
bill or an OOM, not in code review — which is why it's a hard rule here,
not a style preference.

Failure contract
-----------------
Every increment/observe call in this module is a pure in-memory
operation on a `dict`-backed registry — it cannot fail against Redis,
Postgres, or the network the way a push-based metrics client could. It
is still guarded (`_safe`) everywhere it's called from a hot path,
because telemetry must never be the reason a request fails (see the
project's Phase 11 observability principle: best-effort, bounded,
non-blocking).
"""

from __future__ import annotations

from typing import Callable, TypeVar

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.logging.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: A dedicated registry (not the global default) so importing this module
#: twice under different names (e.g. by a test that reloads it) never
#: raises prometheus_client's "duplicated timeseries" error, and so a
#: future second app instance in the same process (workers, tests) can
#: each get their own registry if needed.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------
# HTTP — the RED/golden-signal metrics for the API itself
# ---------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    "nimbusfs_http_requests_total",
    "Total HTTP requests handled.",
    ["method", "route", "status_code"],
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "nimbusfs_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
    registry=REGISTRY,
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "nimbusfs_http_requests_in_progress",
    "HTTP requests currently being handled (saturation signal). Labeled only "
    "by method, not route: the route template isn't known until Starlette's "
    "router resolves it, which happens AFTER this gauge must already be "
    "incremented for an accurate in-flight count.",
    ["method"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------
AUTH_LOGIN_ATTEMPTS_TOTAL = Counter(
    "nimbusfs_auth_login_attempts_total",
    "Login attempts by outcome.",
    ["result"],  # success | failure
    registry=REGISTRY,
)
AUTH_TOKEN_REFRESH_TOTAL = Counter(
    "nimbusfs_auth_token_refresh_total",
    "Refresh-token exchanges by outcome.",
    ["result"],  # success | failure | reuse_detected
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Files (Phase 3 upload/download) + chunked uploads (Phase 6)
# ---------------------------------------------------------------------
FILES_UPLOADED_TOTAL = Counter(
    "nimbusfs_files_uploaded_total",
    "Completed file uploads by outcome.",
    ["result"],  # success | duplicate | failure
    registry=REGISTRY,
)
FILES_DOWNLOADED_TOTAL = Counter(
    "nimbusfs_files_downloaded_total",
    "File download/signed-url operations by outcome.",
    ["result"],  # success | failure
    registry=REGISTRY,
)
FILE_OPERATION_DURATION_SECONDS = Histogram(
    "nimbusfs_file_operation_duration_seconds",
    "Duration of a file storage operation.",
    ["operation"],  # upload | replace | delete
    registry=REGISTRY,
)
UPLOAD_BYTES_TOTAL = Counter(
    "nimbusfs_upload_bytes_total",
    "Total bytes accepted via file uploads (single-shot + chunked, combined).",
    registry=REGISTRY,
)
CHUNKS_UPLOADED_TOTAL = Counter(
    "nimbusfs_chunks_uploaded_total",
    "Chunk uploads by outcome.",
    ["result"],  # success | failure
    registry=REGISTRY,
)
UPLOAD_RESUMPTIONS_TOTAL = Counter(
    "nimbusfs_upload_resumptions_total",
    "Chunked upload sessions resumed after a client disconnect/retry.",
    registry=REGISTRY,
)
ACTIVE_UPLOAD_SESSIONS = Gauge(
    "nimbusfs_active_upload_sessions",
    "Chunked upload sessions currently in progress on this replica (best-effort, in-process).",
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Database (golden-signal: saturation)
# ---------------------------------------------------------------------
DB_POOL_CONNECTIONS = Gauge(
    "nimbusfs_db_pool_connections",
    "SQLAlchemy connection pool state on this replica.",
    ["state"],  # checked_out | checked_in | overflow
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Redis / cache (Phase 7)
# ---------------------------------------------------------------------
CACHE_OPERATIONS_TOTAL = Counter(
    "nimbusfs_cache_operations_total",
    "Cache operations by outcome.",
    ["operation", "result"],  # operation: get|set|delete|invalidate ; result: hit|miss|written|deleted|error
    registry=REGISTRY,
)
RATE_LIMIT_DECISIONS_TOTAL = Counter(
    "nimbusfs_rate_limit_decisions_total",
    "Rate limiter decisions by category and outcome.",
    ["category", "result"],  # result: allowed | rejected | degraded_open | degraded_closed
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Pub/Sub + workers (Phase 8)
# ---------------------------------------------------------------------
PUBSUB_MESSAGES_PUBLISHED_TOTAL = Counter(
    "nimbusfs_pubsub_messages_published_total",
    "Messages published to Pub/Sub by topic and outcome.",
    ["topic", "result"],  # result: success | failure | disabled
    registry=REGISTRY,
)
PUBSUB_MESSAGES_PROCESSED_TOTAL = Counter(
    "nimbusfs_pubsub_messages_processed_total",
    "Messages processed by a worker, by outcome.",
    ["consumer", "result"],  # result: succeeded | failed | duplicate | retried | unparseable
    registry=REGISTRY,
)
PUBSUB_PROCESSING_DURATION_SECONDS = Histogram(
    "nimbusfs_pubsub_processing_duration_seconds",
    "Time spent in a worker's process() call for one message.",
    ["consumer"],
    registry=REGISTRY,
)
WORKER_JOBS_TOTAL = Counter(
    "nimbusfs_worker_jobs_total",
    "Background job outcomes (outbox publisher, reconciliation, etc.).",
    ["worker", "result"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------
# Route-template cache — avoids recomputing per request
# ---------------------------------------------------------------------


def render() -> tuple[bytes, str]:
    """Returns `(body, content_type)` for the `/metrics` endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def safe_call(fn: Callable[[], T], *, operation: str) -> T | None:
    """
    Runs a metrics-recording callable, swallowing (and logging) any
    failure so instrumentation can never break the code path it is
    observing. In practice `prometheus_client` operations essentially
    never raise (pure in-memory dict/lock updates), but the contract in
    `docs/observability.md` ("telemetry is best-effort, bounded,
    non-blocking") is enforced here rather than assumed.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - telemetry must never propagate
        logger.warning("metrics_recording_failed", operation=operation, error=str(exc))
        return None
