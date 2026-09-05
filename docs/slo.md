# NimbusFS — SLIs, SLOs, Error Budget (Phase 11)

Companion to `docs/alerting.md` (alerts derived from these targets) and
`docs/high-availability.md` (the 99.9% availability target Phase 9
already set for the API tier, reused rather than reinvented here — see
§2 below). DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline as in
`docs/observability.md`. **No SLO below has been measured against real
production traffic** — these are targets, not achieved results, per the
brief's explicit instruction: *"Do not claim an SLO is actually achieved
unless it has been measured."*

---

## 1. Definitions (not to be confused with each other)

- **SLI (Service Level Indicator)** — a specific, measured quantity.
  "The proportion of HTTP requests to `/api/v1/*` that complete with a
  non-5xx status code, measured over a rolling 28-day window" is an SLI.
  It is a NUMBER, computed from `nimbusfs_http_requests_total`.
- **SLO (Service Level Objective)** — a target value for an SLI that the
  team commits to internally. "API availability SLI >= 99.9%" is an SLO.
  It is a GOAL, not a guarantee to anyone outside the team.
- **SLA (Service Level Agreement)** — a contractual promise to an
  external party, usually with a financial or credit consequence for
  missing it. NimbusFS has **no SLA today** — there is no external
  customer contract. If one is ever offered, it should be set LOOSER
  than the internal SLO (e.g. an SLA of 99.5% behind a 99.9% SLO),
  never equal to it, so normal operational variance doesn't turn every
  SLO miss into a contractual breach.

## 2. SLIs and SLOs

| SLI | Definition | SLO | Source metric |
|---|---|---|---|
| API availability | % of `/api/v1/*` requests completing non-5xx | **99.9%** monthly (reuses Phase 9's already-set API-tier target — see `docs/high-availability.md` §2 for why not higher, dominated by Cloud SQL failover time) | `nimbusfs_http_requests_total` |
| API latency | % of requests completing under 1s (p95 threshold, route-dependent for upload/download endpoints which are intentionally excluded — see note below) | **95% under 1s**, measured on metadata/auth/folder routes only | `nimbusfs_http_request_duration_seconds` |
| Upload success rate | % of `upload_file`/chunk-upload calls resulting in `result=success` or `result=duplicate` (both are correct outcomes — see `nimbusfs_files_uploaded_total`'s label docstring) | **99.5%** monthly | `nimbusfs_files_uploaded_total`, `nimbusfs_chunks_uploaded_total` |
| Download success rate | % of `get_downloadable_file` calls resulting in `result=success` | **99.5%** monthly | `nimbusfs_files_downloaded_total` |
| Worker processing success | % of Pub/Sub messages resulting in `result=succeeded` or `result=duplicate` (both are correct terminal outcomes) within 3 delivery attempts | **99%** monthly | `nimbusfs_pubsub_messages_processed_total` |
| Pub/Sub processing delay | % of messages processed within 60s of publish | **95%** monthly | `nimbusfs_pubsub_processing_duration_seconds` (a per-message-handler duration, not end-to-end publish-to-ack latency — the true end-to-end figure needs the native Pub/Sub `oldest_unacked_message_age` metric alongside it, since queueing time before a worker even picks up the message isn't captured by this histogram alone) |

**Why upload/download endpoints are excluded from the general API
latency SLO**: their duration is dominated by client upload/download
bandwidth and file size, not server-side processing time — a slow
mobile connection uploading a 2GB file is not a NimbusFS latency
problem, and folding it into one blanket "95% under 1s" SLO would make
the SLO meaningless (either impossibly strict for large files, or so
loose it hides a real regression in small-file/metadata latency). This
is a deliberate SLI-design decision, not an oversight.

## 3. Error budget

At **99.9%** monthly availability, the error budget is:

```
Minutes per 30-day month:        43,200
Allowed downtime (0.1%):         43.2 minutes/month
```

(Matches Phase 9's already-derived annual figure of 8h 45m 56s/year,
÷12 ≈ 43.8 min/month — the small difference is calendar-month-length
rounding, not a different target.)

At **99.5%** (upload/download success rate), the error budget is:

```
Allowed failed operations per 10,000: 50
```

At **99%** (worker processing success), the error budget is:

```
Allowed permanently-failed messages per 10,000: 100
```

### How the error budget governs decisions

- **Deployments**: while the trailing-28-day error budget for API
  availability has meaningful headroom remaining, normal-velocity
  deploys proceed on the existing Phase 5 rolling-update strategy
  (`maxUnavailable: 0`) without extra gating. If the budget is
  **exhausted** (rolling 28-day availability has already dropped below
  99.9%), new feature deploys should pause in favor of reliability work
  until the trailing window recovers — this is a policy recommendation,
  not an automated gate (no CI/CD deployment-freeze automation exists
  in this repo to enforce it mechanically; see `CONTEXT.md`'s "Not yet
  built" list for CI/CD status).
- **Reliability work prioritization**: a budget burning faster than
  linearly (e.g. 50% of the monthly budget consumed in the first week)
  is itself the HIGH-severity "sustained high error rate" alert in
  `docs/alerting.md` — the error budget and the alert catalog are two
  views of the same underlying SLI, not independent systems.
- **Incident response**: every incident's write-up (see
  `docs/incident-response.md`) should state how much error budget it
  consumed — this is what turns "we had an outage" into "we have this
  much budget left this month," the actual operational value of having
  an error budget instead of just an uptime target.
- **Feature velocity**: an SLO is a floor, not a target to hover
  exactly at — a team that is comfortably under budget has room to take
  calculated risks (a riskier deploy, a new dependency); a team that is
  over budget does not, regardless of how much a stakeholder wants a
  feature shipped that week. This is the entire point of an error
  budget over a raw uptime requirement: it converts a binary
  "did we meet the SLA" argument into a shared, quantified resource
  both reliability work and feature work draw from.

## 4. Honesty statement

**None of the SLOs above have been measured against real production
traffic.** No real deployment of NimbusFS has served real user traffic
as of this document. These are TARGETS set from architectural
reasoning (Cloud SQL failover time, GCS/Pub/Sub's own published SLAs,
the retry/circuit-breaker budgets already configured in
`app/core/retry.py`/`app/core/circuit_breaker.py`), not measured
achievements. The correct next step, when real traffic exists, is to
compute each SLI from the metrics in `docs/monitoring.md` §3 over a
real trailing window and compare against these targets — not to assume
they are already met.
