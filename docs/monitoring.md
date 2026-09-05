# NimbusFS — Monitoring (Phase 11)

Companion to `docs/observability.md` (logs/traces/security audit) and
`docs/alerting.md` (turning these metrics into alert policies). Same
DESIGNED/IMPLEMENTED/TESTED/MEASURED labeling discipline throughout —
see `docs/observability.md`'s opening section for the definitions.
**Nothing quantitative in this document is MEASURED** — no real GKE/
Cloud SQL/Memorystore/Cloud Monitoring project was available this
session.

---

## 1. Google Cloud Monitoring vs. Prometheus/Grafana — the comparison the brief requires before choosing

| | Self-hosted Prometheus + Grafana | Google Managed Prometheus (GMP) + Cloud Monitoring (chosen) |
|---|---|---|
| New infrastructure to run | A Prometheus server Deployment/StatefulSet (with a PersistentVolume for its TSDB), an Alertmanager Deployment, a Grafana Deployment, all needing their own scaling/patching/backup story | None — GMP is a GKE-managed collector DaemonSet already available on-cluster; Cloud Monitoring/Logging are already the destination for every other GCP-native signal this project depends on (Cloud SQL, Memorystore, GKE itself) |
| Storage/retention ownership | This project would own Prometheus's local TSDB retention and (for durability) a remote-write target | Cloud Monitoring owns retention; no PVC, no capacity planning for metrics storage |
| Correlating app metrics with GKE/Cloud SQL/Memorystore's own metrics | Requires either a separate Grafana datasource per system or a federation setup | Automatic — everything lands in the same Cloud Monitoring project/workspace, one query surface |
| Alerting integration | Alertmanager's own routing/notification config, separate from anything GCP-native | Cloud Monitoring alert policies — same system used for GKE/Cloud SQL/Memorystore alerts already, one on-call surface |
| Exposition format required from the app | Prometheus text format | Prometheus text format — **identical requirement**, which is exactly why choosing GMP costs nothing extra: `prometheus_client`'s output is unmodified between the two options |
| Operational cost this phase is willing to add | The brief explicitly says not to add infrastructure "simply because it is commonly used" | Zero new Deployments; a `PodMonitoring` CRD instance is the only new object |

**Conclusion: GMP + Cloud Monitoring, not self-hosted Prometheus/Grafana.**
The two are not actually in tension here — GMP *is* Prometheus (the
same scrape/storage/query model), managed by GKE instead of by this
project. A self-hosted Prometheus/Grafana pair would be justified if
NimbusFS needed a Grafana-specific dashboarding feature Cloud Monitoring
genuinely lacks, or ran outside GKE — neither is true today, so it is
not introduced. This decision is recorded here, not assumed, per the
brief's explicit requirement to justify the choice before making it.

## 2. What `prometheus_client` actually is here

`app/core/metrics.py` — an in-process registry (`CollectorRegistry`),
not a server. `GET /metrics` (`app/api/observability_routes.py`) renders
it as Prometheus text on demand; nothing is pushed anywhere by the app
itself. See `docs/observability.md` §9 for the failure-containment
contract (`safe_call`) and cardinality discipline.

## 3. Metric inventory (bounded labels only — see the cardinality note in `app/core/metrics.py`)

| Metric | Type | Labels | Golden signal / RED axis |
|---|---|---|---|
| `nimbusfs_http_requests_total` | Counter | `method`, `route` (template), `status_code` | Traffic, Errors |
| `nimbusfs_http_request_duration_seconds` | Histogram | `method`, `route` | Latency |
| `nimbusfs_http_requests_in_progress` | Gauge | `method` | Saturation |
| `nimbusfs_auth_login_attempts_total` | Counter | `result` (success\|failure) | Security / Errors |
| `nimbusfs_auth_token_refresh_total` | Counter | `result` (success\|failure\|reuse_detected) | Security |
| `nimbusfs_files_uploaded_total` | Counter | `result` (success\|duplicate\|failure) | Traffic, Errors |
| `nimbusfs_files_downloaded_total` | Counter | `result` (success\|failure) | Traffic, Errors |
| `nimbusfs_file_operation_duration_seconds` | Histogram | `operation` (upload) | Latency |
| `nimbusfs_upload_bytes_total` | Counter | — | Throughput |
| `nimbusfs_chunks_uploaded_total` | Counter | `result` (success\|failure) | Traffic, Errors |
| `nimbusfs_upload_resumptions_total` | Counter | — | Reliability (client retry/disconnect rate) |
| `nimbusfs_active_upload_sessions` | Gauge | — | Saturation (declared; not yet wired to a live count — see Remaining Risks) |
| `nimbusfs_db_pool_connections` | Gauge | `state` (checked_out\|checked_in\|overflow) | Saturation |
| `nimbusfs_cache_operations_total` | Counter | `operation` (get\|set\|delete), `result` (hit\|miss\|written\|deleted\|error) | Errors, effectiveness |
| `nimbusfs_rate_limit_decisions_total` | Counter | `category`, `result` (allowed\|rejected\|degraded_open\|degraded_closed) | Security, Saturation |
| `nimbusfs_pubsub_messages_published_total` | Counter | `topic`, `result` (success\|failure\|disabled) | Traffic, Errors |
| `nimbusfs_pubsub_messages_processed_total` | Counter | `consumer`, `result` (succeeded\|failed\|duplicate\|retried) | RED (rate/errors) for workers |
| `nimbusfs_pubsub_processing_duration_seconds` | Histogram | `consumer` | RED (duration) for workers |
| `nimbusfs_worker_jobs_total` | Counter | `worker`, `result` | RED (rate/errors) for the outbox publisher |

**Why no `chunk_failures_total` distinct from `chunks_uploaded_total{result=failure}`,
no separate `download_duration_seconds`, etc.**: several metrics the
Phase 11 brief lists by name (§14) are folded into a smaller set of
more general metrics with a `result`/`operation` label rather than one
counter per named failure mode — this is the SAME reasoning the brief
itself gives for bounded labels (§11): a `result` label with 2-4 fixed
values is one time series family, not N separate metric names to keep
in sync forever as failure modes are added.

## 4. What's still measured only in logs, deliberately, not as a metric

- **Per-query database latency** — no `db_query_duration_seconds`
  metric. Only pool saturation (`nimbusfs_db_pool_connections`) is a
  metric; individual query timing is a log-and-trace concern
  (`app/core/tracing.py` spans would be the correct place if/when this
  is added), not a metric, specifically to avoid the temptation to
  label it by table/operation in a way that creeps toward high
  cardinality.
- **GCS operation latency** — captured as `span_completed`/
  `span_failed` log events with `duration_ms` (see
  `docs/observability.md` §5), not as a Prometheus histogram. A
  `gcs_operation_duration_seconds{operation=...}` histogram is a
  reasonable next metric (bounded — `operation` is a small fixed set)
  and is the clearest "not yet done, but obviously should be" gap in
  this phase's metric inventory.
- **Redis command-level latency** — `CacheService` already logs
  `duration_ms` on every operation (Phase 7); Phase 11 added the
  `nimbusfs_cache_operations_total` counter but not a duration
  histogram, for the same reason as GCS above.

## 5. Golden signals / RED, applied

| Component | Latency | Traffic | Errors | Saturation |
|---|---|---|---|---|
| API (FastAPI) | `nimbusfs_http_request_duration_seconds` | `nimbusfs_http_requests_total` | `status_code` label on the above | `nimbusfs_http_requests_in_progress`, `nimbusfs_db_pool_connections` |
| Workers (Pub/Sub consumers) | `nimbusfs_pubsub_processing_duration_seconds` | `nimbusfs_pubsub_messages_processed_total` | `result=failed` on the above | `WORKER_CONCURRENCY` (config, not yet a live gauge — Remaining Risks) |
| Outbox publisher | — (single-row publish, not latency-sensitive) | `nimbusfs_worker_jobs_total`, `nimbusfs_pubsub_messages_published_total` | `result=failed`/`failure` on the above | outbox backlog (not yet a metric — see below) |
| Redis (cache) | not a metric (see §4) | `nimbusfs_cache_operations_total` | `result=error` | Memorystore's own native Cloud Monitoring metrics (memory, connections) — NimbusFS does not duplicate infrastructure-level Redis metrics the managed service already exports |
| GCS | not a metric (see §4) | inferred from `nimbusfs_files_uploaded_total`/`upload_bytes_total` | inferred from `result=failure` labels upstream | GCS has no meaningful per-app saturation signal to expose (it's a managed multi-tenant service) |
| PostgreSQL | not a metric (app-side) | inferred from HTTP traffic | inferred from HTTP `5xx` | `nimbusfs_db_pool_connections`; Cloud SQL's own native metrics (CPU, connections, disk) cover the rest |

## 6. Database, Redis, GCS, Pub/Sub, Kubernetes/GKE monitoring — what NimbusFS adds vs. what's already native

Per the brief's own instruction (§21: "prefer native GCP observability
when it reduces unnecessary operational complexity"), NimbusFS does
**not** re-implement infrastructure-level monitoring Cloud SQL,
Memorystore, GCS, Pub/Sub, and GKE already export natively to Cloud
Monitoring (CPU, memory, disk, replication lag, connection counts,
subscription backlog/oldest-unacked-message-age, Pod restart counts,
OOMKills, node capacity, HPA activity). NimbusFS's own metrics (§3)
exist specifically to cover the gap those native signals cannot see:
**what the application itself is doing** (which operation, which
outcome, how long from the app's own perspective) — the two are meant
to be viewed together in Cloud Monitoring, not duplicated.

**Pub/Sub backlog specifically**: `subscription/num_undelivered_messages`
and `subscription/oldest_unacked_message_age` are native Pub/Sub metrics
already available in Cloud Monitoring without any code in this repo —
`docs/alerting.md` alerts on them directly rather than NimbusFS
reimplementing a backlog gauge the platform already computes correctly
(computing it app-side would require polling the subscription's own
admin API on a schedule, adding latency/cost to answer a question Cloud
Monitoring already answers for free).

## 7. Dashboards

Designed as Cloud Monitoring dashboards (JSON dashboard definitions are
not included in this repo — no live Cloud Monitoring workspace exists
to author/validate them against; each is DESIGNED only). One dashboard
per operationally-meaningful concern, per the brief's "do not create a
dashboard with hundreds of metrics" instruction:

1. **System Overview** — `nimbusfs_http_requests_total` (rate, by
   `status_code` class 2xx/4xx/5xx), `nimbusfs_http_request_duration_seconds`
   (p50/p95/p99), replica count vs. desired (native GKE), `/ready` probe
   failure rate (native GKE).
2. **API detail** — the same, broken down by `route` (bounded — one row
   per endpoint template) and `method`.
3. **File Storage** — `nimbusfs_files_uploaded_total`/`files_downloaded_total`
   (by `result`), `nimbusfs_upload_bytes_total` rate, `nimbusfs_chunks_uploaded_total`,
   `nimbusfs_upload_resumptions_total`.
4. **Workers / Event Pipeline** — `nimbusfs_pubsub_messages_processed_total`
   (by `consumer`/`result`), `nimbusfs_pubsub_processing_duration_seconds`,
   `nimbusfs_worker_jobs_total`, native Pub/Sub subscription backlog +
   oldest-unacked-age per subscription.
5. **Infrastructure** — native GKE (Pod count/restarts/OOMKills, node
   CPU/memory), native Cloud SQL (CPU, connections, storage), native
   Memorystore (memory, connections, evictions), `nimbusfs_db_pool_connections`.
6. **Security** — `nimbusfs_auth_login_attempts_total{result=failure}`,
   `nimbusfs_auth_token_refresh_total{result=reuse_detected}`,
   `nimbusfs_rate_limit_decisions_total{result=rejected}`, plus the
   Phase 10 `AuditLog` table (queried directly, not a metric — audit
   events are inherently high-cardinality/per-actor and belong in a
   queryable log store, not a Prometheus label).

## 8. Load testing (Phase 11 §36)

NimbusFS already has load-test scaffolding from earlier phases
(`scripts/load-test/`, k6/Locust scripts from Phase 6). This phase adds
no new load-test tooling — extending the existing one was preferred per
the brief's "if load-testing infrastructure already exists, extend it"
instruction, and no new scenario was actually run against real
infrastructure this session (none was available).

- **TEST DESIGNED**: run the existing `scripts/load-test/` k6/Locust
  scripts against a real deployment while watching `nimbusfs_http_request_duration_seconds`
  p50/p95/p99, `nimbusfs_http_requests_total` error rate,
  `nimbusfs_db_pool_connections`, and Cloud SQL/Memorystore's native
  connection-count metrics simultaneously in the "System Overview" +
  "Infrastructure" dashboards above.
- **TEST EXECUTED**: no. No real GKE/Cloud SQL/Memorystore instance was
  available this session.
- **TEST RESULT**: none — not fabricated. Any specific
  requests/second, latency percentile, or error-rate number for
  NimbusFS under load is unmeasured as of this document and must not be
  asserted until a real run against real infrastructure produces one.
