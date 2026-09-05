# NimbusFS — Observability (Phase 11)

Companion to `docs/monitoring.md` (metrics/dashboards/infrastructure
monitoring detail), `docs/alerting.md` (alert policies), `docs/slo.md`
(SLI/SLO/error budget), and `docs/incident-response.md` (how an engineer
actually uses all of this during an incident). Same bluntness about gaps
as `docs/high-availability.md`/`docs/disaster-recovery.md`.

**Read every claim below through the same lens Phase 9 established**:
**DESIGNED**, **IMPLEMENTED**, **TESTED**, or **MEASURED**. A metric
name existing is DESIGNED. Code that increments it on a real code path
is IMPLEMENTED. A test asserting that in `tests/test_observability.py`
is TESTED. A number obtained from a real production incident or load
test against real infrastructure is MEASURED. As of this Phase 11
session, **nothing quantitative here is MEASURED** — no real GKE
cluster, Cloud SQL, Memorystore, or Cloud Monitoring project was
available (the same constraint every prior phase has recorded). The
logging/metrics/tracing/health mechanisms themselves are IMPLEMENTED
and TESTED against fakes (see "Testing" below); the GKE/Cloud Monitoring
integration (PodMonitoring, alert policies) is DESIGNED and syntax-
validated only, never applied to a real cluster.

---

## 1. Repository inspection — what already existed before this phase

A mandatory inspection (per the Phase 11 brief) found NimbusFS's
observability posture, built incrementally since Phase 4, already
substantially real, not a green-field problem:

- **Structured JSON logging** (`app/logging/logger.py`, `structlog`,
  since Phase 4) — every log line already `structlog`-shaped, JSON in
  non-dev environments, human-readable console output in dev.
- **Correlation/trace/server IDs** (`app/middleware/request_context.py`,
  Phase 4) — `request_id` (per-hop), `correlation_id` (per client
  operation, honors an inbound `X-Correlation-ID`), `trace_id` (honors
  `X-Trace-ID`, defaulted to `correlation_id` pre-Phase-11), and
  `server_id`/`hostname`, all bound into `structlog.contextvars` for the
  whole request and echoed back as response headers.
- **Correlation/causation propagated through the event pipeline**
  (`app/events/envelope.py`, `app/events/emitter.py`, Phase 8) — every
  outbox event carries `correlation_id` (read from the same contextvars
  at emit time) and `causation_id` (the parent event's ID), and
  `BaseWorker._handle` (`app/workers/base.py`) rebinds them into the
  worker's own logging context per message.
- **Three-tier health endpoints already correctly separated**
  (`app/api/v1/health/routes.py`, Phase 4): `/live` (no dependency
  checks — answers "should Kubernetes restart this process?"), `/ready`
  (DB+Redis+Storage checks, 503 when not ready — "should this Pod
  receive traffic?"), `/health` (same checks, richer diagnostic body,
  for humans/dashboards). This is exactly the liveness-vs-readiness
  separation §22 of the Phase 11 brief asks to verify, and it was
  already correct — nothing needed to change here.
- **A `/metrics` placeholder already anticipated in the deployment
  manifest**: `k8s/07-deployment.yaml`'s `prometheus.io/path` annotation
  carried the comment `"placeholder until a real /metrics endpoint
  exists (future phase)"` — literally this phase's job, now filled in.
- **Extensive structured operational logging with an explicit,
  documented decision to defer metrics**: `app/services/cache_service.py`
  and `app/core/rate_limiter.py`'s module docstrings both state a full
  Prometheus/OpenTelemetry stack was "explicitly out of scope" for
  Phases 7, deferred to a metrics-focused future phase — i.e. this one.

**What was genuinely missing**, confirmed by grep/read, not assumed:
a `/metrics` endpoint, any metrics library, any span/tracing primitive,
a `trace_id` propagated across the Pub/Sub hop (each worker previously
started a fresh implicit trace context per message with no link back to
the causing HTTP request), and any of the artifacts §37 of the brief
lists (`docs/observability.md` etc. did not exist).

---

## 2. Security audit of existing logging (Phase 11 §3)

Grepped every `logger.*(...)` / `structlog` call site in `app/` for
password/token/credential/signed-URL exposure before writing a single
line of new instrumentation, per the brief's ordering ("audit before
implement"). Findings:

| Location | Type | Severity | Remediation |
|---|---|---|---|
| No call site found passing a raw password, JWT, refresh token, database/Redis connection string, or a *complete* signed URL to a log call. | — | — | — |
| `app/services/storage_service.py`'s signed-URL logging (`signed_url_generation_started/completed`) logs `object_name` only, never the URL itself. | Confirmed safe | — | No action. |
| `app/core/rate_limiter.py` logs a SHA-256 **hash** of the rate-limit identity (user ID or IP), never the raw value. | Confirmed safe | — | No action. |
| A **future** call site, or a merged-in third-party log record, could still bind a field under a sensitive name and this codebase had no structural guard against it — everything above was "careful by discipline," not "impossible by construction." | Real gap (defense-in-depth) | Low | **Fixed this phase**: `app/logging/logger.py::_redact_sensitive_fields`, a `structlog` processor run last in the chain, replaces the VALUE of any event-dict key matching a fixed, lower-cased set (`password`, `access_token`, `refresh_token`, `authorization`, `jwt`, `secret`, `api_key`, `signed_url`, `database_url`, `redis_url`, …) with `***REDACTED***`, unconditionally. Matches on key name, not a value-content regex, so it cannot false-negative on an unexpected but sensitively-named field, and cannot false-positive on legitimately non-secret content. Tested in `tests/test_observability.py::TestRedaction`. |

No actual secret value is reproduced anywhere in this document or in
the codebase's tests — the tests use obviously-fake literals
(`"hunter2"`, `"eyJhbGciOi..."`).

---

## 3. The three pillars, and how they connect

```
Client
  |
  v
Load Balancer (GCLB)
  |
  v
FastAPI replica ---------------------------------------------+
  | RequestContextMiddleware: request_id / correlation_id /   |
  |   trace_id / server_id bound into structlog.contextvars   |
  | MetricsMiddleware: RED metrics, bounded labels             |
  |                                                             |
  +--> PostgreSQL   (spans: none yet — see "Remaining Risks")   |
  +--> Redis         CacheService: cache_hit/miss/error logs   |
  |                   + nimbusfs_cache_operations_total         |
  +--> GCS           StorageService: gcs.* spans (span_id/      |
  |                   parent_span_id/duration_ms logged)        |
  +--> Pub/Sub (publish)                                        |
         EventEnvelope carries correlation_id/causation_id/     |
         trace_id (Phase 11: NEW field) -----------------+      |
                                                          |      |
                                                          v      |
                                                       Worker    |
                                             BaseWorker._handle  |
                                             rebinds trace_id/   |
                                             correlation_id into |
                                             its OWN contextvars,|
                                             so its logs (and    |
                                             gcs.* spans it      |
                                             triggers) join the  |
                                             SAME trace_id the   |
                                             original HTTP       |
                                             request generated.  |
                                                                 |
Every hop's `/metrics` -----------------------------------------+
  scraped by Google Managed Prometheus (k8s/24-podmonitoring.yaml)
  -> Cloud Monitoring
```

- **Logs** ("what happened?") — structured JSON, `structlog`, already
  ingested by Cloud Logging with zero extra shipping code (stdout is
  the entire contract on GKE).
- **Metrics** ("how much/how often?") — `app/core/metrics.py`,
  `prometheus_client`, scraped by Google Managed Prometheus, exposed at
  `GET /metrics`.
- **Traces** ("where did the time go?") — `app/core/tracing.py`'s
  span primitive: named, timed, nested sub-operations logged as
  `span_started`/`span_completed`/`span_failed`, carrying `trace_id`/
  `span_id`/`parent_span_id`, queryable in Cloud Logging by `trace_id`.
  See §5 below for why this is a deliberately lighter-weight choice than
  the full OpenTelemetry SDK.

## 4. Correlation IDs (Phase 11 §7 — already built, verified still correct)

Three distinct IDs, unchanged by this phase except where noted:

- `request_id` — unique per HTTP hop, never client-supplied (prevents
  header-injection into logs).
- `correlation_id` — unique per end-to-end client operation, honors an
  inbound `X-Correlation-ID`.
- `trace_id` — **Phase 11 change**: previously reserved-but-unused
  beyond "defaults to `correlation_id`"; now genuinely propagated across
  the Pub/Sub hop (see §5). Still honors an inbound `X-Trace-ID` at the
  HTTP edge.

All four (including `server_id`) are logged automatically on every line
via `structlog.contextvars` — no call site threads them manually.

## 5. Distributed tracing (Phase 11 §8-9) — design rationale

**Why a hand-rolled span/trace-ID primitive instead of the OpenTelemetry
SDK.** The Phase 11 brief is explicit: *"Do not introduce ...
OpenTelemetry ... simply because it is commonly used. Every additional
component must have a clear architectural justification."* Weighed here:

| | Full OpenTelemetry SDK + Cloud Trace exporter | `app/core/tracing.py` (chosen) |
|---|---|---|
| New dependencies | `opentelemetry-api`, `-sdk`, `-instrumentation-fastapi`/`-sqlalchemy`/`-redis`, an exporter package | None (uses `structlog`, already a dependency) |
| Trace-waterfall UI | Yes, in Cloud Trace | No — spans are structured log lines, queried by `trace_id` in Cloud Logging |
| Effort to verify in THIS session | Cannot be verified end-to-end: no real GCP project/credentials exist in this environment (same constraint every phase has hit), so an exporter wire-up would be unverified, unlike every other phase's tests-against-fakes discipline | Fully testable against fakes today — no exporter, no network call, nothing to fake |
| Propagation across Pub/Sub | Would need `traceparent` injected into Pub/Sub message attributes and extracted worker-side — genuinely new plumbing either way | The same plumbing, but into the ALREADY-EXISTING `EventEnvelope`/`correlation_id` mechanism (Phase 8) — one new field (`trace_id`), not a new propagation system |
| Migration cost later | — | Every span call site (`start_span("operation", ...)`) already marks exactly where a real OTel span should start/stop — swapping the implementation is mechanical, not a redesign |

The decision: ship the lightweight primitive now, record the real gap
honestly (below), and leave a mechanical migration path rather than
either skipping tracing entirely or bolting on an unverified dependency
under time/infrastructure constraints this phase can't relax.

**What it does**: `app/core/tracing.py::start_span(operation, **fields)`
is a context manager. On entry it generates a short `span_id`, reads the
enclosing span (if any) as `parent_span_id`, binds both into
`structlog.contextvars` for the duration of the `with` block (so nested
spans and any log line inside automatically carry them), and logs
`span_started`. On exit it logs `span_completed` with `duration_ms`, or
`span_failed` (still with `duration_ms`) if the block raised, then
restores the enclosing context. Wired into `StorageService`'s GCS calls
today (`gcs.upload`, `gcs.download_range`, `gcs.delete`,
`gcs.generate_signed_url`, `gcs.compose_objects`) — the actual slow,
externally-dependent operations, not "a span for every function" (which
the brief explicitly warns against).

**Cross-Pub/Sub propagation** (Phase 11 §9, the specific ask): the
`EventEnvelope` gained a `trace_id: str | None` field
(`app/events/envelope.py`), populated at emit time
(`app/events/emitter.py::_emit_event`) by reading the SAME
`structlog.contextvars` the HTTP request already bound. `BaseWorker._handle`
(`app/workers/base.py`) rebinds that trace_id — not a fresh one — into
the worker's own contextvars before calling `process()`. Result: an
engineer can take the `X-Trace-ID` a client received on its upload
response, grep Cloud Logging for it, and see the HTTP request, the
outbox-publish log line, the Pub/Sub `event_published`, and the
worker's `event_processing_*`/`span_*` lines — one query, one chain —
without a trace-waterfall UI. `to_pubsub_message()` also puts `trace_id`
in the Pub/Sub message attributes (when present) so it's visible without
decoding the payload.

**Sampling** (§32): none is implemented — every request/span is logged
today. This is a stated, deliberate trade-off for THIS phase's scale
(no production traffic exists to sample), not a claim that 100%
sampling is the right permanent choice. The forward path when log
volume/cost becomes a real constraint (§33):
- **Head sampling** — a probability decided at the `RequestContextMiddleware`
  entry point, e.g. sample 1-in-N by hashing `trace_id`.
- **Error sampling** — always keep 100% of non-2xx responses and
  `span_failed` events regardless of the head-sampling decision (a
  request "sampled out" that then errors is the single worst case to
  lose visibility into).
- **High-latency sampling** — always keep requests exceeding a p99-ish
  threshold, for the same reason.
None of this is implemented — it's the honest next step, not a
retrofit-later footgun, since the span/log shapes already carry
everything a sampler would need to decide on (`duration_ms`, status).

## 6. Structured logging (Phase 11 §6) — hardening applied

- `_redact_sensitive_fields` (§2 above) — new.
- No change to the existing field shape (`timestamp`, `level`,
  `logger`, `event`, plus whatever `structlog.contextvars` has bound) —
  it was already close to the brief's example shape; adding a rigid
  schema would have meant touching every one of the hundreds of
  pre-existing `logger.*()` call sites for no operational benefit.
- Deliberately did NOT log every field the brief's example shows
  (`file_id`, `user_id` on every line) — those already appear on the
  SPECIFIC log lines that need them (e.g. `upload_completed`), not
  globally; adding them to the global contextvars binding would put
  unbounded-cardinality values in scope for anything that later reads
  contextvars into a metric label by mistake (see §7's cardinality
  discussion) — logs are the right place for them, contextvars-for-every-line
  is not, and Phase 4 already made this distinction correctly.

## 7. Metrics — see `docs/monitoring.md` for the full inventory, dashboard, and Cloud-Monitoring-vs-Prometheus comparison.

## 8. Health endpoints (Phase 11 §22-23) — reviewed, unchanged

Already correct (see §1). Specifically verified against the brief's
requirements during this phase:
- Liveness does **not** depend on DB/Redis/Storage — confirmed by
  reading `app/api/v1/health/routes.py::liveness_check`, which
  constructs its response with no `await` on any dependency check at
  all. A Cloud SQL blip cannot cause a liveness-probe-triggered restart
  storm.
- Readiness and health share the same three dependency checks but
  differ in probe-shape (503 vs. always-200-with-a-status-field) — this
  is the correct split (§22): a load balancer wants a boolean-shaped
  signal, a human wants detail.
- Dependency checks use the SAME retry/timeout policy already
  established in Phase 4 (`Settings.RETRY_MAX_ATTEMPTS` etc.) — no new
  "expensive health check called every second" pattern was introduced.

No code change was made here. Documented as reviewed, not silently
skipped, per the brief's acceptance checklist.

## 9. Telemetry-failure containment (Phase 11 §31, §11 cardinality)

- Every metrics call in this codebase goes through
  `app/core/metrics.py::safe_call`, which catches and logs (never
  raises) any exception from a `prometheus_client` operation. In
  practice these are pure in-memory dict/lock updates and essentially
  never fail, but the CONTRACT (never let telemetry become a failure
  amplifier) is enforced in code, not assumed.
- Every span-boundary log call in `app/core/tracing.py::start_span` is
  individually wrapped in `try/except: pass` — a logging-backend hiccup
  during a span can never turn into a NEW exception distinct from
  whatever the wrapped business code itself raised or didn't.
- Metric label sets are a fixed, small, code-controlled enumeration
  everywhere (`method`, route TEMPLATE, `status_code`, `operation`,
  `result`, `category`, `consumer`, `worker`) — see
  `app/core/metrics.py`'s module docstring for the full cardinality-risk
  explanation (an unbounded label like `user_id` creates a permanent new
  time series per value, a slow memory/cost leak discovered in a bill or
  an OOM, not in code review).
- `/metrics` itself never touches Postgres/Redis/GCS except a
  best-effort, `safe_call`-guarded read of the SQLAlchemy connection
  pool's own in-process counters (`checkedout()`/`checkedin()`/
  `overflow()` — no I/O) — a scrape can never be slow because a
  downstream dependency is slow.

## 10. Testing (Phase 11 §35)

`tests/test_observability.py` (20 tests, all against fakes/in-process
state — no real Prometheus/Cloud Trace/Cloud Logging):
- **Redaction**: known sensitive keys are replaced; unrelated fields are
  untouched; matching is case-insensitive.
- **Metrics**: the registry renders valid Prometheus text exposition
  format; the declared label sets for the HTTP/cache/Pub/Sub-processing
  counters are exactly the bounded sets documented (a literal assertion
  that would fail immediately if someone added a `user_id` label);
  `safe_call` swallows an exception and returns `None`, and returns the
  real value on success; `GET /metrics` returns 200 with the right
  content type through the real FastAPI app; a real HTTP request
  measurably increments `nimbusfs_http_requests_total` with a route
  TEMPLATE label; a 404 to an unmatched path is labeled `"unmatched"`,
  never the literal requested path (the cardinality guarantee, proven,
  not just asserted in a docstring).
- **Tracing**: span IDs are unique; `start_span` binds and restores
  `structlog.contextvars` correctly, including nested parent/child spans
  and restoration after an exception; `current_trace_id()` reads the
  bound contextvar.
- **Envelope propagation**: `trace_id` defaults to `None`; is included
  in Pub/Sub attributes only when present; round-trips through
  JSON serialization; and an end-to-end HTTP upload test confirms the
  response's `X-Trace-ID` header is present (the same value
  `_emit_event` would have read at emit time).

Every pre-existing test (429 before this phase) still passes unchanged
— **449/449 passing** after this phase, zero regressions. Run:
`pytest -q` from `nimbusfs/`.

## 11. Remaining risks (recorded honestly, not fixed)

- **No real OpenTelemetry/Cloud Trace integration** — see §5's
  comparison. The span primitive here is a genuine, tested, useful
  approximation, not equivalent to a trace-waterfall UI.
- **No sampling** — every request is fully logged/spanned today; a real
  production traffic volume would need §5's sampling strategy
  implemented, not just described.
- **`/metrics` has no application-level auth** — by design (matches
  every standard Prometheus-style exporter), but this means the actual
  access control is 100% the NetworkPolicy in `k8s/11-networkpolicy.yaml`
  plus the absence of any public Ingress path to it
  (`k8s/15-ingress.yaml` only routes `/api/v1/*`). If either of those
  ever changes, `/metrics`'s exposure surface changes with them — this
  is a real coupling, not a defense-in-depth redundancy.
- **`PodMonitoring`/alert policies are never applied to a real
  cluster** — syntax-validated (`yaml.safe_load`) only, exactly like
  every other `k8s/*.yaml` and `terraform/*.tf` file added since Phase 5.
- **No load test was run in this phase** — see `docs/monitoring.md`
  §"Load Testing" for what's designed vs. executed.
- **DB query-level latency is not instrumented** — only pool
  saturation (`nimbusfs_db_pool_connections`) and the request-level RED
  metrics. Per-query histograms would need SQLAlchemy event-listener
  instrumentation, deliberately deferred rather than adding an
  unbounded-cardinality `query` label by mistake.
