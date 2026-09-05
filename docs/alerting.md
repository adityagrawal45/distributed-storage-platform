# NimbusFS — Alerting (Phase 11)

Companion to `docs/monitoring.md` (the metrics these alerts read) and
`docs/slo.md` (the error budget that governs how sensitive these
thresholds should be). DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline
as in `docs/observability.md`. **Every alert policy below is DESIGNED
only** — none has been created in a real Cloud Monitoring workspace or
has ever actually fired, since no real GCP project/traffic exists for
this session. `terraform/monitoring.tf` encodes them declaratively. Unlike Phase 9's
Terraform extension, `terraform validate`/`plan` were **not** re-run
against this file in this session (no `terraform` binary was installed
this session) — it is reviewed by eye against the
`google_monitoring_alert_policy`/`google_monitoring_uptime_check_config`
resource schemas, not machine-validated. Treat it as a lower confidence
level than the rest of `terraform/`, and run `terraform validate`
before relying on it.

---

## 1. Alert-fatigue policy (Phase 11 §27)

Every alert below is `condition + threshold + duration + severity`, not
`1 error = 1 alert`. Every alert answers, explicitly, three questions:
**What happened? Why does it matter? What should the engineer do?** — a
policy missing any of the three is not shipped. Transient single-request
errors are absorbed by the DURATION requirement; a genuinely single
failed request never pages anyone.

## 2. Alert catalog

### CRITICAL — pages immediately

| Alert | Condition | Duration | Why it matters | First action |
|---|---|---|---|---|
| API unavailable | `nimbusfs_http_requests_total{status_code=~"5.."}` rate / total rate > 50% | 2 min | Users cannot use the product at all | `docs/incident-response.md` §"API unavailable" |
| No healthy replicas | Available replicas (native GKE) = 0 | 1 min | Total outage, not degraded — the Deployment itself is failing | Check `kubectl get pods -n nimbusfs`, `kubectl describe deployment nimbusfs-api` |
| Database unavailable | `/ready` failure rate (native GKE probe metric, or `nimbusfs_http_requests_total{route="/api/v1/ready",status_code="503"}`) = 100% | 2 min | Every write-touching request fails | Check Cloud SQL instance status; see `docs/disaster-recovery.md` if regional |
| Severe data-processing failure | `nimbusfs_pubsub_messages_processed_total{result="failed"}` rate > 25% of total processed, any single `consumer` | 5 min | A worker is systematically failing, not just retrying transient errors | Check the specific worker's logs for `event_permanently_failed`/`event_processing_failed_will_retry` |

### HIGH — pages during business hours, escalates if unacked

| Alert | Condition | Duration | Why it matters | First action |
|---|---|---|---|---|
| Sustained high error rate | `nimbusfs_http_requests_total{status_code=~"5.."}` rate / total rate > 5% | 5 min | Below "unavailable" but a real, ongoing user-facing problem | `docs/incident-response.md` §"High API error rate" |
| Severe latency increase | `nimbusfs_http_request_duration_seconds` p95 > 2x its trailing 7-day baseline for the same route | 10 min | A meaningful fraction of users experiencing slow responses | `docs/incident-response.md` §"API latency spike" |
| Worker backlog | native Pub/Sub `subscription/num_undelivered_messages` growing monotonically | 15 min | Consumers are falling behind production — user-visible effects (thumbnails/notifications) lag | Check worker replica health/CPU; consider scaling worker Deployment |
| Pub/Sub oldest-unacked-message age | native `subscription/oldest_unacked_message_age` > `ack_deadline` * 3 | 10 min | A message is being redelivered repeatedly without succeeding — likely a poison message or a systemic consumer bug | Check the specific message's `event_id` in worker logs |
| Database connection exhaustion | `nimbusfs_db_pool_connections{state="checked_out"}` at or near `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW` sustained | 5 min | Next request queues or times out waiting for a connection | Check for a long-running/leaked transaction; see `app/database/session.py`'s pool-sizing note on the N-replica ceiling |

### MEDIUM — ticket, not a page

| Alert | Condition | Duration | Why it matters | First action |
|---|---|---|---|---|
| Cache degradation | `nimbusfs_cache_operations_total{result="error"}` rate > 10% of total cache ops | 10 min | Every request is falling through to Postgres at higher latency — not an outage (Phase 7's degrade-to-source contract holds) but a real performance regression | Check Memorystore native metrics (memory/connections/evictions) |
| Increasing worker retries | `nimbusfs_pubsub_messages_processed_total{result="retried"}` rate trending up over 1h | 30 min | Precedes a HIGH-severity backlog alert if unaddressed | Check the specific worker's error logs for a new recurring failure |
| Disk growth | Cloud SQL native disk-usage metric trending toward autoresize threshold | 1h | Avoid an unplanned resize event or, worse, hitting the ceiling | Review recent write volume; check for an unbounded table (e.g. `AuditLog`) needing a retention policy |
| Elevated upload failures | `nimbusfs_files_uploaded_total{result="failure"}` rate / total > 2% | 15 min | Below HIGH's blanket error-rate threshold but specific to the core product feature | Check `FileValidationService`/`StorageService` logs for a specific failure mode (bad MIME sniffing, GCS permission drift) |

## 3. What is deliberately NOT alerted on

- **Any single failed request** — the whole point of the duration
  requirement.
- **`nimbusfs_rate_limit_decisions_total{result="rejected"}` in
  isolation** — a rejected request is the rate limiter doing its job
  correctly (protecting the system from abuse), not a failure; only
  `degraded_open`/`degraded_closed` (Redis itself failing) is
  alert-worthy, and only at HIGH via the "cache degradation"-shaped
  alert if it correlates with Redis errors generally.
- **`nimbusfs_auth_login_attempts_total{result="failure"}` in
  isolation** — expected background noise (typos, expired sessions);
  a spike relative to baseline is a security-monitoring concern for a
  human to review via the Security dashboard, not an automated page,
  since the appropriate response (is this credential stuffing? one
  confused user?) requires judgment an alert threshold can't encode
  well without a real baseline to tune against.

## 4. Uptime checks

DESIGNED: a Cloud Monitoring synthetic uptime check against
`GET /api/v1/live` (never `/health`/`/ready` — an uptime check should
answer "is the edge reachable at all", not "are all dependencies up",
which is what the CRITICAL "Database unavailable" alert above already
covers separately) from 3+ regions, checked every 60s, feeding the
"API unavailable" CRITICAL alert above as a second, independent signal
alongside the error-rate-based one (an uptime check catches a total
network/LB failure that never even reaches the point of incrementing
`nimbusfs_http_requests_total`).
