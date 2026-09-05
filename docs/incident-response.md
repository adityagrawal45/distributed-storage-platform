# NimbusFS — Incident Response & Investigation (Phase 11)

Companion to `docs/observability.md` (the mechanisms used below),
`docs/monitoring.md` (dashboards referenced), `docs/alerting.md` (what
paged the engineer in the first place), and `docs/slo.md`/`docs/failure-testing.md`
(Phase 9 — post-incident error-budget accounting and chaos-test
procedures). DESIGNED/IMPLEMENTED/TESTED discipline as elsewhere —
**these workflows have never been exercised against a real production
incident**, only reasoned through from the actually-implemented
logs/metrics/spans (Phase 11) and the actually-implemented failure
scenarios from Phase 9's `docs/failure-testing.md`.

---

## 1. General investigation shape

Every workflow below follows the same funnel, from broad to specific:

```
Dashboard (docs/monitoring.md)
   |  "which signal moved, and when"
   v
Metric breakdown (by route/consumer/category — bounded labels only)
   |  "which specific endpoint/worker/category"
   v
Logs, filtered by the metric's dimensions + time window
   |  "what actually happened, with request_id/trace_id"
   v
Trace reconstruction: grep Cloud Logging for one `trace_id`
   |  "the full HTTP request -> outbox -> Pub/Sub -> worker chain"
   v
Span timeline within that trace_id (span_started/span_completed/span_failed)
   |  "which specific sub-operation (a GCS call, a DB write) took the time / failed"
   v
Root cause
```

## 2. Scenario: API latency spike (matches the brief's §29 example exactly)

1. **Dashboard**: "System Overview" shows `nimbusfs_http_request_duration_seconds`
   p95 elevated above baseline.
2. **API metrics**: break down by `route` (bounded label) in the "API
   detail" dashboard — is it one endpoint or all of them? All-endpoints
   points at a shared dependency (DB pool, Redis, node CPU); one
   endpoint points at that endpoint's own logic.
3. **Trace**: pick a few slow requests' `X-Trace-ID` from the access
   logs (`request_completed` events with high `duration_ms`) in the
   affected window.
4. **Database/Redis/GCS span**: for each `trace_id`, look at the
   `span_completed`/`span_failed` log lines bound to it — a `gcs.upload`
   span with an outsized `duration_ms` points at GCS; no spans at all
   during the slow window, with the time instead sitting between
   `request_started` and the first DB/Redis/GCS-touching log line,
   points at the database itself (no span is emitted around raw
   SQLAlchemy queries yet — see `docs/observability.md`'s Remaining
   Risks — so a DB-bound spike is inferred by exclusion today, not
   directly spanned).
5. **Logs**: read the full structured log line for that `trace_id`'s
   slow request — `duration_ms` fields at each stage, `cache_hit`/
   `cache_miss` (a cache-miss storm inflates DB load), rate-limiter
   `rate_limit_degraded` (a Redis outage that widened the limiter's
   own latency).
6. **Root cause**: stated as a specific finding (e.g. "cache miss rate
   jumped from 5% to 60% at 14:02 UTC, correlating with a
   `cache_invalidated` burst from a bulk folder-move operation") —
   never "it was slow," which gives the next engineer nothing to act on.

## 3. Scenario: High API error rate

1. **Dashboard/alert**: `docs/alerting.md`'s "Sustained high error rate"
   fires.
2. **Breakdown**: `nimbusfs_http_requests_total{status_code=~"5.."}` by
   `route` — concentrated on one route (a specific bug/dependency) vs.
   spread across all routes (a shared-dependency outage, e.g. Postgres).
3. **Exception handler logs**: every domain exception in
   `app/exceptions/handlers.py` logs its own structured event on the
   way out (e.g. `storage_exception_handler`, `unhandled_exception_handler`)
   — filter by the affected route + time window to get the actual
   exception type/message without needing a debugger attached to a
   production Pod.
4. **Correlate with dependency health**: check `/health`'s component
   breakdown (`database`/`redis`/`storage`) if reachable, and the
   native Cloud SQL/Memorystore/GCS dashboards, for a simultaneous
   dependency-side incident.

## 4. Scenario: Worker backlog / dead-letter growth

1. **Dashboard/alert**: `docs/alerting.md`'s "Worker backlog" or
   "Pub/Sub oldest-unacked-message age" fires (native Pub/Sub metrics —
   see `docs/monitoring.md` §6 for why NimbusFS doesn't duplicate these).
2. **Which consumer**: `nimbusfs_pubsub_messages_processed_total` broken
   down by `consumer` — which worker (file-processing/thumbnail/
   notification) is behind.
3. **Retry vs. genuine stall**: `result="retried"` climbing with
   `result="succeeded"` near zero for that consumer means every message
   is failing and being redelivered (check that worker's
   `event_processing_failed_will_retry` log lines for the actual
   exception) — vs. all results near zero, meaning the worker process
   itself isn't running (`kubectl get pods` for that worker's
   Deployment, check for a crash loop).
4. **Poison message**: a single `event_id` appearing repeatedly in
   `event_processing_failed_will_retry` logs across many delivery
   attempts (`delivery_attempt` field, bound by `BaseWorker._handle`) —
   the NonRetryableEventError path should have caught this; its absence
   suggests a bug in a worker's `process()`'s exception classification,
   worth its own follow-up rather than being treated as capacity/scaling.
5. **Note what NimbusFS does NOT have**: no dead-letter-queue replay
   tooling exists yet (recorded as a gap since Phase 8) — a
   permanently-failed (`NonRetryableEventError`) message is ACKed and
   recorded in `ProcessedEvent` with `status=FAILED`, queryable directly
   from Postgres, but there is no automated re-drive mechanism. Recovery
   today is manual: query `ProcessedEvent` for `status=FAILED` rows in
   the affected window, fix the root cause, and (if the underlying
   business event still needs to happen) manually re-trigger the
   originating action.

## 5. Failure scenarios: which ones observability actually detects (Phase 11 §30)

Cross-referencing Phase 9's `docs/failure-testing.md` failure matrix
against what THIS phase's logs/metrics/spans would actually surface,
honestly (not asserting detection for a mechanism that doesn't exist):

| Scenario | Detected by | Confidence |
|---|---|---|
| FastAPI Pod crash | Native GKE Pod-restart-count metric; `nimbusfs_http_requests_in_progress` on that Pod drops to 0 without a corresponding `request_completed` for in-flight requests | High — native K8s signal is authoritative |
| Node failure | Native GKE node-health/Pod-eviction metrics | High — native signal |
| Database connection failure | `/ready` returns 503 (`database` component `unhealthy`); `nimbusfs_http_requests_total{status_code="503"}` for `/api/v1/ready`; `nimbusfs_db_pool_connections` may show exhaustion first if it's a slow degradation rather than a hard failure | High |
| Redis failure | `nimbusfs_cache_operations_total{result="error"}` spikes; `rate_limit_degraded` log events; requests keep succeeding (degrade-to-source contract, Phase 7) — so this is a PERFORMANCE-DEGRADATION detection, not an outage detection, by design | High for detection, correctly does NOT look like an outage |
| GCS request failure | `nimbusfs_files_uploaded_total{result="failure"}`/`files_downloaded_total{result="failure"}`; `gcs.*` `span_failed` log events with the specific GCS exception type | High |
| Pub/Sub failure (publish side) | `nimbusfs_pubsub_messages_published_total{result="failure"}`; `event_publish_failed` logs | High |
| Worker crash | Native GKE Pod-restart-count for that worker Deployment; heartbeat-file liveness probe (Phase 8) failing | High — relies on the existing Phase 8 heartbeat mechanism, unchanged this phase |
| Worker retry storm | `nimbusfs_pubsub_messages_processed_total{result="retried"}` rate spike | High |
| Dead-letter growth | **Not directly** — no DLQ topic/metric exists (see §4 above); `ProcessedEvent{status=FAILED}` rows are queryable but not exposed as a metric | **Gap, recorded honestly** |
| High API latency | `nimbusfs_http_request_duration_seconds` | High |
| High API error rate | `nimbusfs_http_requests_total{status_code=~"5.."}` | High |
| Resource exhaustion (CPU/memory) | Native GKE Pod CPU/memory metrics feeding the existing HPA (Phase 5); `nimbusfs_http_requests_in_progress` climbing without a corresponding throughput increase is a secondary app-level signal | High via native metrics, medium via the app-level secondary signal alone |

**This table itself was not produced by actually injecting these
failures against a running NimbusFS this session** — it is a reasoned
mapping from "what log/metric would this failure necessarily touch,
given the code that's actually there" to a confidence level, following
the same honesty discipline as Phase 9's chaos-testing procedures
(`docs/failure-testing.md`), which labels its own scenarios
LOCAL/STAGING/PRODUCTION rather than claiming any were run. The
NonRetryableEventError/DLQ gap above is real, not a hedge.

## 6. Post-incident

Every incident write-up should record, at minimum: the trigger alert
(if any) or how it was otherwise discovered, the `trace_id`(s)
investigated, the root cause in the specific terms §2's example shows
(not "it was slow"/"it errored"), the error-budget consumption (see
`docs/slo.md` §3), and — if a gap in observability itself contributed to
a slower-than-necessary diagnosis — a follow-up to close that gap,
cross-referenced against `docs/observability.md`'s "Remaining Risks".
