# NimbusFS — High Availability (Phase 9)

Companion to `docs/disaster-recovery.md` (regional/catastrophic failure)
and `docs/failure-testing.md` (how every claim below is meant to be
verified). Same bluntness about gaps as `docs/PHASE_7_REDIS_DESIGN.md`
and `docs/event-driven-architecture.md`.

**Read this document's claims through one lens throughout**: every
statement below is labeled **DESIGNED**, **IMPLEMENTED**, **TESTED**, or
**MEASURED**. A config file existing is DESIGNED. Code that enforces it
is IMPLEMENTED. A test exercising it in this repo's suite is TESTED. A
number obtained from a real failure/restore against real infrastructure
is MEASURED. As of this Phase 9 session, **nothing in this document is
MEASURED** — no real GKE cluster, Cloud SQL instance, or Memorystore
instance was available (same constraint every prior phase has stated).
Everything here is DESIGNED, most of it is IMPLEMENTED in application
code or manifests, and the parts covered by `tests/test_reconciliation.py`
and the pre-existing Phase 4/6/7/8 failure-injection suites are TESTED
against fakes — never against the real managed services this document
describes.

---

## 1. High Availability vs. Disaster Recovery

These are two different guarantees, deliberately kept separate rather
than folded into one "reliability" chapter:

| | High Availability | Disaster Recovery |
|---|---|---|
| **Answers** | "Does the system keep serving traffic through a normal, expected infrastructure failure?" | "Can the system be brought back after a failure too large for HA to absorb?" |
| **Failure scope** | Pod crash, node crash, single zone outage, a Redis/Cloud SQL blip | An entire GCP region unavailable, a corrupted database, a fat-fingered mass delete |
| **Mechanism** | Redundancy + automatic failover *within* the running system (extra replicas, extra zones, managed-service HA) | Restore *from durable, independently-stored state* into a different environment |
| **Time scale** | Seconds to low minutes, no human required | Minutes to hours, usually with a human executing a runbook |
| **This phase's docs** | This file | `docs/disaster-recovery.md` |

A system can have excellent HA and terrible DR (three zones, zero
backups) or the reverse (nightly backups, no redundancy — every blip is
an outage). NimbusFS Phase 9 builds both, but they are evaluated and
tested independently for exactly this reason: conflating them is how a
"we have backups" claim gets mistaken for "we can survive a zone
outage," or vice versa.

---

## 2. Availability target

**Target: 99.9% monthly, for the API tier.** Not 99.95%, not 99.99%.

Annual downtime budget at 99.9%: **8h 45m 56s/year** (≈43m 50s/month).

Why not higher:
- The dominant availability ceiling in this architecture is **Cloud SQL
  regional HA's failover time** (typically well under a minute, but not
  instant — see §5), which recurs on every unplanned primary failure and
  every planned maintenance window that requires failover. A handful of
  such events per year already consumes a meaningful fraction of a
  99.95%+ budget (99.95% = 4h 22m 58s/year; 99.99% = 52m 35s/year).
- 99.99%+ realistically requires either eliminating Cloud SQL failover
  time from the critical path (e.g. read replicas serving reads during
  failover — designed-but-not-wired since Phase 4, see README §11) or
  accepting an architecture (multi-region active-active with a
  distributed database) far more complex than this project's current
  single-region-multi-zone shape. Claiming 99.99% on top of a
  single-writer regional Postgres would be a number the architecture
  cannot actually back.
- Workers (Phase 8) are explicitly **not** counted in this SLO. A worker
  outage delays thumbnails/notifications, which degrades a feature; it
  never turns into an API 5xx, so mixing worker uptime into the same
  number would just make an unrelated component look like it's failing
  the API's SLO.

Why not lower: single-zone, single-replica deployments (no HA effort at
all) sit closer to 99.0–99.5% in practice once you count routine node
maintenance and deploys — worse than what §3–§9 below cost to build.

**Status: DESIGNED.** No SLO dashboard exists yet (out of scope — no
observability stack this phase, see README §23), so this number is a
target the architecture is built to support, not a measured actual.

---

## 3. Target architecture

```
                                Internet
                                    |
                                    v
                    Global External Application Load Balancer
                          (GKE Ingress, 15-ingress.yaml)
                                    |
                +-------------------+-------------------+
                |                   |                   |
                v                   v                   v
          GKE Zone A           GKE Zone B           GKE Zone C
       nimbusfs-api pods    nimbusfs-api pods    nimbusfs-api pods
        (topology spread constraints + soft anti-affinity, §4)
                |                   |                   |
                +-------------------+-------------------+
                                    |
        +---------------------------+---------------------------+
        |                           |                           |
        v                           v                           v
   Cloud SQL (regional HA)   Memorystore (Standard tier,   GCS (regional or
   primary + standby,        replica + automatic failover)  dual-region bucket,
   auto-failover, §5              §8                         §11)
        |                           |                           |
        +---------------------------+---------------------------+
                                    |
                                Pub/Sub
                        (regional, managed, §9)
                                    |
                +-------------------+-------------------+-------------------+
                |                   |                   |
                v                   v                   v                   v
        outbox-publisher       file-worker        thumbnail-worker    notification-worker
         (2 replicas,          (2 replicas,          (2 replicas,          (2 replicas,
          Phase 9)              Phase 8)              Phase 8)              Phase 9)
        each spread across zones via topologySpreadConstraints (§4, k8s/18-21)
```

This is a **single-region, multi-zone** architecture, not multi-region.
Multi-region is `docs/disaster-recovery.md`'s subject, deliberately kept
out of the HA layer — see §1's table and §17 there for why active-active
was rejected.

**Status: DESIGNED + IMPLEMENTED** (k8s manifests, application code).
**Not TESTED/MEASURED** — see §16.

---

## 4. Multi-zone GKE

NimbusFS Phase 5 already assumed a **regional GKE cluster** (nodes
spanning 3 zones in one region) — this phase makes that assumption
explicit and closes the gap between "soft anti-affinity" and "guaranteed
even spread":

- **Regional GKE cluster**: the control plane itself is replicated across
  3 zones by GKE (this is what "regional cluster" means, as opposed to
  "zonal cluster" — a zonal cluster's control plane is a single point of
  failure independent of anything below). `k8s/README.md`'s cluster-
  creation step must use `--region`, not `--zone`.
- **Node pools**: `nimbusfs-app-pool` (07-deployment.yaml's preferred
  `nodeAffinity` target) must itself span all 3 zones — a regional
  cluster with a zonal node pool still has a single-zone blast radius for
  every Pod on it. The Cluster Autoscaler (Phase 5) scales this pool per-
  zone independently.
- **Pod scheduling**: two complementary mechanisms, not one —
  - `podAntiAffinity` (Phase 5, `preferred...`) expresses a *relative*
    preference: "prefer not to land where this Deployment's other Pods
    already are," weighted zone-first (100) then host-second (50).
  - `topologySpreadConstraints` (**Phase 9**, `07-deployment.yaml` and
    `k8s/18-21`) expresses an *absolute* bound: `maxSkew: 1` on
    `topology.kubernetes.io/zone` means no zone may hold more than one
    Pod more than the least-loaded zone, at any replica count the HPA
    scales to. Anti-affinity alone tends to clump once one zone looks
    "different enough" to satisfy the preference; skew doesn't have that
    failure mode. Both are `ScheduleAnyway`/`preferred...` (soft), for
    the same reason Phase 5 chose soft over hard: a temporarily degraded
    zone must never leave a Pod stuck `Pending` instead of running
    somewhere.
- **PodDisruptionBudget**: `10-pdb.yaml` (`minAvailable: 2`, API) unchanged
  from Phase 5; **Phase 9 adds** `23-pdb-workers.yaml` (`minAvailable: 1`
  per worker Deployment) now that every worker runs >=2 replicas (§7).
- **Cluster Autoscaler**: unchanged from Phase 5 — scales node count per
  zone in response to unschedulable Pods, which is what makes
  `topologySpreadConstraints`'s soft guarantee actually achievable rather
  than aspirational under load.
- **HPA**: unchanged from Phase 5 (`09-hpa.yaml`, 3→10 replicas on
  CPU+memory). Phase 9 does not change the HPA — the floor of 3 already
  guarantees at least one replica per zone can exist; scaling logic
  itself is orthogonal to *where* replicas land, which is what §4's
  mechanisms answer.

Example distribution at `replicas: 6` (mid-HPA-range):

```
Zone A:  api-pod-1  api-pod-4
Zone B:  api-pod-2  api-pod-5
Zone C:  api-pod-3  api-pod-6
```

**Status: DESIGNED + IMPLEMENTED** (manifests). **Not TESTED/MEASURED**
against a real regional cluster — see §16 for the exact `kubectl`
commands a future session with cluster access should run.

---

## 5. Cloud SQL high availability

NimbusFS uses **Cloud SQL for PostgreSQL, Regional (HA) configuration**:
one primary in zone A, one standby in zone B (same region), continuous
synchronous-replication-backed standby maintained by the Cloud SQL
control plane (not application-visible logical replication — this is
Google's managed HA mechanism, distinct from a read replica).

**What automatic failover does and does not mean**:
- On primary failure (VM crash, zone outage, some maintenance events),
  Cloud SQL promotes the standby and repoints the instance's private IP
  — the application's `POSTGRES_HOST` **does not change**, because it
  already points at the *instance*, not a specific VM.
- **Failover is not instantaneous.** Google's documented typical range is
  under a minute for the promotion itself, but the *application-visible*
  outage window also includes: existing connections to the old primary
  erroring out, `asyncpg`'s connection pool (Phase 1) discovering the
  break, and `run_with_deadlock_retry`/the Phase 4 `retry_async` wrapper
  (`app/database/session.py`) retrying with backoff until a new
  connection to the (now-promoted) standby succeeds. Realistic end-to-end
  application-visible impact: **tens of seconds of elevated error rates
  and latency**, not zero. Any claim of "seamless" failover is the thing
  §16's real-failover test exists to check, because nothing in this
  session backs that claim with a measurement.
- The **replica itself is not directly queryable** by the application in
  this phase — there is no read/write split (Phase 4 documented, not
  wired — README §11). The standby exists purely for failover, not for
  offloading read traffic. Wiring reads to a replica is a legitimate
  future optimization, not a Phase 9 requirement.

**Configuration**:
- `availabilityType: REGIONAL` (not `ZONAL`) on the Cloud SQL instance.
- Automated daily backups, enabled (§6).
- **Point-in-time recovery (PITR) enabled** — requires binary logging
  (`enable_point_in_time_recovery: true`), retained for the same window
  as the backup retention policy below.
- Maintenance window pinned to a low-traffic period, `updateTrack: stable`
  (not `canary`) — production databases should not be Google's first
  exposure to a new Cloud SQL patch.
- Backup retention: **14 days** (see §6 for the reasoning).

None of this is new application code — it is Cloud SQL instance
configuration (`gcloud sql instances create/patch`), which is why it
lives in this document rather than in `k8s/` or `app/`. A future
Terraform phase (README §23, still out of scope) would encode it as IaC;
this phase documents the equivalent `gcloud` commands in
`docs/backup-restore.md` instead.

**Status: DESIGNED, documented.** **Not IMPLEMENTED against a real Cloud
SQL instance** in this session (none available) — same caveat as every
Phase 5 GKE claim.

---

## 6. Database backups — see `docs/backup-restore.md`

Full backup/PITR strategy, retention rationale, and the executable
restore-test procedure live in `docs/backup-restore.md` rather than
duplicated here, per this document's own principle in §1: HA and backup/
restore answer different questions and are easy to conflate if
interleaved.

---

## 7. Redis (Memorystore) high availability

**Tier: Standard** (replica + automatic failover), not Basic.

- Basic tier is a single node with no replica — any maintenance event or
  node failure is a hard Redis outage until a new instance is
  provisioned (minutes, not seconds), which for NimbusFS means: caches
  go cold (tolerable, Phase 7's whole design assumes this can happen at
  any time) but **distributed locks and rate limiting also go dark
  simultaneously** for that whole window (less tolerable — see §8).
- Standard tier adds a same-region cross-zone replica with automatic
  failover on primary failure, typically **under 60 seconds** per
  Google's documented behavior, without requiring a new instance
  provision.
- Memorystore's client-facing endpoint does not change across failover
  (same IP), so `app/database/redis.py`'s existing `redis.asyncio`
  connection pool — already configured with `REDIS_SOCKET_TIMEOUT_SECONDS`
  / `retry_on_timeout` / `health_check_interval` since Phase 7 — is
  sufficient application-side; no new reconnection code is needed for
  Memorystore failover specifically. This is the payoff of Phase 7's "one
  Redis pool, one place to size" decision (README §14) recurring here.
- **What is lost on failover, always**: any key written between the last
  replication cycle and the failure (Memorystore Standard replication is
  asynchronous, not synchronous — unlike Cloud SQL regional HA's stronger
  guarantee). This is acceptable specifically *because* Redis "owns
  nothing" (Phase 7's governing invariant, README §14) — a lost cache
  entry costs one Postgres read on the next request; a lost lock or
  rate-limit bucket state is bounded by the TTLs already in place
  (`LOCK_DEFAULT_TTL_SECONDS`, the rate limiter's `capacity`/`window`), not
  a correctness violation.

**Status: DESIGNED, documented.** Not provisioned in this session.

---

## 8. Redis failure handling (in-application)

This is the one part of this document that is genuinely **IMPLEMENTED
and TESTED** already, because Phase 7 built it before Phase 9 existed —
this section is Phase 9 confirming and cross-referencing that work
rather than adding new code:

| Redis role | On Redis unavailable | Where |
|---|---|---|
| Cache (user/folder/file/search) | Every `CacheService` method catches the failure, logs it, returns "as if the cache did not exist" — **falls through to Postgres**, never raises | `app/services/cache_service.py`, tested in `tests/test_caching.py` |
| Distributed locks (correctness-critical, e.g. chunk-upload coordination) | `DistributedLockService`/`ChunkedUploadService._guarded_lock` translate the infra failure into `ServiceUnavailableException` (503) at ACQUISITION — **fails safe, never proceeds as if the lock were held** | `app/core/distributed_lock.py`, `app/services/chunked_upload_service.py`, tested in `tests/test_distributed.py`/`tests/test_chunked_upload.py` |
| Rate limiting | Configurable: **fail-open by default** (`RATE_LIMIT_FAIL_OPEN=true`), fail-closed available and tested | `app/core/rate_limiter.py`, tested in `tests/test_rate_limiting.py` |

**Security implication of fail-open rate limiting** (explicit, per this
phase's ask): during a Redis outage, abuse-rate protection on
login/register/upload endpoints is temporarily absent. This is an
accepted trade-off, not an oversight — Phase 7's reasoning (README §14)
is that rate limiting here is abuse mitigation sitting behind a load
balancer, not an authorization control, and failing closed would turn
every Redis blip into a fleet-wide 429 storm for legitimate users
mid-upload. `RATE_LIMIT_FAIL_OPEN=false` is a one-line config change for
any deployment that judges its own risk tolerance differently (e.g. a
public-registration endpoint under active credential-stuffing attack).

**Status: IMPLEMENTED + TESTED** (against `FakeRedisClient`'s failure
injection, not real Memorystore).

---

## 9. Pub/Sub resilience & worker resilience

Unchanged from Phase 8, restated here because it is exactly what makes
this layer's HA story different from Cloud SQL's: **the outbox pattern
means a Pub/Sub or worker outage never loses data, only delays it.**

- **Publisher crash**: outbox rows are written transactionally with the
  business data (README §15's whole point) — a crashed API pod or outbox
  publisher leaves rows durably `PENDING` in Postgres, picked up by the
  next successful poll. No event is ever lost between commit and publish.
- **Pub/Sub temporary outage**: `outbox_publisher.py`'s poll loop survives
  any exception (including "Pub/Sub unreachable") and retries on the next
  interval — see its own docstring, "the LOOP must survive anything."
- **Worker crash mid-message**: the message is redelivered by Pub/Sub
  after the ack deadline; `ProcessedEvent`'s `UniqueConstraint(event_id,
  consumer_name)` makes redelivery a no-op rather than a duplicate side
  effect (README §15, tested in `tests/test_base_worker.py`).
- **Phase 9 change**: `outbox-publisher` and `notification-worker` moved
  from 1 replica to 2 (§ below and `k8s/18`/`k8s/21`) specifically so a
  single node/zone failure does not stall an entire pipeline stage until
  a replacement Pod reschedules — see each file's Phase 9 comment for the
  full reasoning. `file-worker`/`thumbnail-worker` were already at 2
  replicas in Phase 8; Phase 9 only adds zone-level spread to them.
- **Delayed delivery**: bounded by `MAX_DELIVERY_ATTEMPTS`/DLQ routing,
  unchanged from Phase 8 (README §15.13's classification table).

**Status: IMPLEMENTED + TESTED** for the application-level guarantees
(`tests/test_outbox_publisher_worker.py`, `tests/test_events_integration.py`).
**Not MEASURED** against real Pub/Sub (no emulator or real service started
in any Phase 8 or Phase 9 session — see README §15.16).

---

## 10. Failure matrix

| Component | Failure | Impact | Detection | Recovery | Data at risk |
|---|---|---|---|---|---|
| API Pod | Crash | Low — other replicas absorb traffic | Liveness probe (`/live`) | kubelet restarts container | None (stateless) |
| API Pod | Unresponsive/slow dependency | Low-Medium | Readiness probe (`/ready`) fails | Removed from Service endpoints, not restarted; rejoins on next pass | None |
| Node | Crash | Medium — Pods on it become unavailable | Node NotReady (kubelet heartbeat timeout, ~40s default) | GKE reschedules Pods elsewhere; PDB paces *voluntary* drains only | None |
| Zone | Outage | High — up to 1/3 of capacity gone | GCLB health checks fail for that zone's backends | Traffic + Pod scheduling shift to remaining zones (§4) | None if capacity in other zones is sufficient |
| Cloud SQL primary | Crash/zone failure | High — writes blocked until failover | Cloud SQL internal health checks | Automatic promotion of standby, <60s typical (§5) | None committed; in-flight uncommitted transactions rolled back |
| Memorystore | Primary failure | Medium — cache/locks/rate-limit unavailable during failover | `CacheService`/`RateLimiter` catch connection errors | Automatic replica promotion, <60s typical (§7); app degrades per §8 in the interim | None (Redis owns nothing) |
| Pub/Sub | Temporary outage | Medium — event delivery paused | Publish/ack call failures, logged | Outbox retains rows; publisher retries; consumers catch up on recovery (§9) | None (outbox durability) |
| GCS | Object/API error | High for the affected request | `StorageException` subclasses on the call | `retry_async` (transient) / `CircuitBreaker` (Compose calls); user-visible error if exhausted | None to existing objects; the in-flight operation fails |
| Worker (any of 4) | Pod crash mid-message | Medium — one message delayed | Pub/Sub redelivery after ack deadline; heartbeat-file liveness probe for a wedged process | Redelivered to a healthy replica; `ProcessedEvent` makes it idempotent (§9) | None |
| Region | Outage | Critical — whole system down | External/global health checks (out of DR-region monitoring) | Disaster recovery procedure — see `docs/disaster-recovery.md` | Bounded by RPO (see DR doc) |

---

## 11. Monitoring preparation

No observability stack ships this phase (explicitly out of scope, same
as Phase 5/7/8) — this is the metric *inventory* a future Phase's
Cloud Monitoring/OpenTelemetry work should wire up, not a running
dashboard:

- Pod availability: ready-replica count vs. desired, per component
  (`kube_deployment_status_replicas_available` in GKE's built-in
  Cloud Monitoring integration — no app code needed).
- Node/zone availability: node count Ready per zone, from GKE's built-in
  node metrics.
- Database availability: Cloud SQL's built-in `database/up` metric +
  failover event log entries.
- Redis availability: Memorystore's built-in `memorystore.googleapis.com/
  stats/connections/*` + failover events.
- GCS errors: `storage_service.py`'s existing structured logs
  (`storage_*_failed` events) already carry everything a log-based metric
  needs — no code change, just a log-based metric definition.
- Pub/Sub backlog: `num_undelivered_messages` per subscription (built-in
  Pub/Sub metric) — the same metric §"Not yet built" in README §23 names
  as the natural signal for future backlog-based worker autoscaling.
- Worker failures: `ProcessedEvent(status=FAILED)` row count (a SQL
  query today; a log-based/DB-exporter metric in a future phase).
- Backup failures: Cloud SQL backup operation status (built-in).
- Failover events: Cloud SQL + Memorystore operation logs (built-in,
  visible in Cloud Logging without new code).

---

## 12. Alerting design

| Alert | Severity | Why |
|---|---|---|
| Zero healthy API replicas (readiness) | **P0** | Total outage |
| API replica count below PDB `minAvailable` for >5 min | **P1** | Approaching the outage threshold |
| Cloud SQL failover event | **P1** | Confirms an HA event occurred even if the app degraded gracefully — needs a human to confirm no lingering effect |
| Memorystore failover event | **P2** | Degraded, not down (§8) |
| Pub/Sub backlog above a sustained threshold | **P2** | Delayed side effects, not data loss |
| Dead-letter topic depth growing | **P2** | Real, permanently-failed messages accumulating — needs triage, not urgent |
| Backup failure (any single day) | **P1** | Directly widens RPO exposure — see DR doc |
| GCS elevated error rate | **P1** | Uploads/downloads failing for users |
| API 5xx rate above baseline | **P0/P1** depending on magnitude | Direct user impact |

Severity legend: **P0** page immediately, full outage or imminent.
**P1** page during business hours / urgent same-day. **P2** ticket,
next business day. **P3** informational, no page.

**Status: DESIGNED** (this table). **Not IMPLEMENTED** — no alerting
backend exists in this codebase or session (out of scope, §"Restrictions").

---

## 13. Security during HA events

Failover must not create a security regression:

- **IAM/Workload Identity**: unaffected by any HA event described here —
  KSA→GSA bindings are Kubernetes/IAM state, not tied to which zone a Pod
  lands in or which Cloud SQL replica is currently primary.
- **Secret Manager**: unaffected — secrets are fetched by reference
  (`06-secret.example.yaml` documents the shape; real secrets are never
  committed), not cached in a way a failover could serve stale/wrong
  credentials from.
- **TLS/private networking**: Cloud SQL private IP and Memorystore both
  keep the same private IP across their own internal failover (§5, §7) —
  no NetworkPolicy or firewall rule needs to change, and none should.
- **Cloud Armor**: out of scope this phase (README §23), so there is
  nothing here to regress — noted for completeness, not a gap this phase
  claims to close.
- **Authentication/authorization**: entirely stateless (JWT, Phase 1) and
  unaffected by any of the failures in §10 — a Cloud SQL failover cannot
  make an expired-but-not-yet-rejected token valid, nor can a Redis
  outage bypass `get_current_user`'s deliberate non-cached Postgres check
  (README §14's "user cache is deliberately NOT on the auth path").

**Status: DESIGNED** — none of this required new code; it is a property
of decisions already made in Phases 1–8, restated here as a Phase 9
verification pass rather than new work.

---

## 14. Cost analysis: single-zone vs. multi-zone vs. multi-region

Architecture-level comparison only — **no fabricated GCP pricing**. Use
the GCP Pricing Calculator with actual instance sizes/traffic for real
numbers; this table states cost *drivers*, not dollar figures.

| | Single-zone | Multi-zone (this phase) | Multi-region (DR doc) |
|---|---|---|---|
| Compute | N pods, 1 zone | Same N pods, spread 3 zones — **no extra compute cost**, same total replica count | +M pods in a second region (warm standby, see DR doc) — real added cost |
| Database | Zonal Cloud SQL | Regional (HA) Cloud SQL — **roughly 2x** the zonal instance cost (standby is a real, billed VM) | + cross-region backup storage/egress for DR |
| Redis | Basic tier | Standard tier — **more than 2x** Basic (replica is billed) | Unchanged unless DR mandates a second instance (this design does not, see DR doc) |
| Storage | Regional bucket | Unchanged (bucket location is independent of GKE zone spread) | Dual-region or multi-region bucket — meaningfully higher $/GB than regional (§ GCS doc) |
| Network egress | Minimal (single zone) | **Inter-zone traffic** between Pods and zonal Cloud SQL/Memorystore replicas — small but nonzero, GCP does not charge for same-region cross-zone in most configurations as of this writing (verify current pricing) | Cross-region replication/egress — the largest new line item of the three columns |
| Operational complexity | Lowest | Moderate — this phase's manifests, no new operational processes | Highest — a second environment, a failover runbook, DNS changes (DR doc §18) |

**Recommendation**: multi-zone (this phase) is the right default for
NimbusFS today — it removes the single biggest availability risk (one
zone's node pool or the zonal instances themselves) at a bounded, mostly
compute-neutral cost. Multi-region is justified only if the business
accepts DR-doc-level operational complexity for RTO/RPO targets a
single-region regional-HA setup cannot meet — see `docs/disaster-
recovery.md` §"Design Decisions" for that call.

---

## 15. Design decisions

- **Soft (`preferred...`/`ScheduleAnyway`) scheduling constraints
  throughout, never hard.** Every hard alternative considered
  (`required...` anti-affinity, `whenUnsatisfiable: DoNotSchedule`) was
  rejected for the same reason Phase 5 rejected hard anti-affinity: with
  a bounded number of zones/nodes, a hard constraint can leave a Pod
  permanently `Pending` during exactly the kind of degraded-capacity
  event HA is meant to survive.
- **`topologySpreadConstraints` added alongside, not instead of, Phase
  5's `podAntiAffinity`.** They solve different problems (§4) and the
  existing anti-affinity already works; removing it to "replace" it with
  spread constraints would have been change for its own sake.
- **Two workers bumped from 1→2 replicas (outbox-publisher,
  notification-worker), two left alone (file-worker, thumbnail-worker
  were already 2).** This is a minimal, explained, backward-compatible
  change (per this phase's own instruction) rather than a blanket
  "everything gets N replicas" policy — each Deployment's own header
  comment carries the specific reasoning.
- **No read replica wiring this phase**, despite Cloud SQL HA giving one
  a standby to potentially route reads to. Phase 4 already documented why
  (README §11: `FileMetadata.version` isn't a lock counter, no
  read/write-split code exists) and this phase's scope is HA/DR, not
  read-path optimization — conflating them would be scope creep the
  user's prompt explicitly warns against ("do not introduce
  infrastructure simply because it sounds enterprise-grade").
- **A CronJob, not a long-running worker, for reconciliation** — see
  `app/workers/reconciliation_job.py`'s docstring; restated here because
  it is itself an HA-adjacent decision (a one-shot batch job has a
  trivially simple failure mode: it either completes or its Job is
  marked Failed, versus a long-running process needing its own liveness
  story).

---

## 16. How to actually verify this document's claims

Every "DESIGNED, not MEASURED" line above becomes MEASURED only by
running these against real infrastructure — see `docs/failure-
testing.md` for the full numbered procedure list and environment
labeling (LOCAL/STAGING/PRODUCTION):

1. `kubectl get pods -o wide` across a real regional cluster, confirm the
   zone distribution matches §4's example.
2. `kubectl delete pod <one-api-pod>` — confirm no dropped requests via a
   concurrent load generator, per k8s/README.md's existing self-healing
   demo (Phase 5), unchanged this phase.
3. Drain/cordon a whole zone's nodes (`kubectl drain <node> --ignore-
   daemonsets` for every node in one zone, or the GKE-native "simulate
   zone outage" pattern) — confirm remaining zones absorb traffic and
   measure the actual request-error window, if any.
4. `gcloud sql instances failover <instance>` against a real regional
   Cloud SQL instance — measure the real application-visible error
   window, do not assume §5's "typical" numbers apply to this specific
   instance size/load.
5. Force a Memorystore Standard-tier failover (via a maintenance
   operation or documented `gcloud` failover trigger) — measure the real
   cache/lock/rate-limit degradation window against §8's code paths.

## 17. Interview questions

**Beginner**
- What is the difference between a liveness probe and a readiness probe,
  and why does NimbusFS's API deployment use different endpoints for
  each?
- Why does a stateless application make rolling deployments safer?

**Intermediate**
- Why is `podAntiAffinity` alone insufficient to guarantee an even
  cross-zone Pod distribution, and what does `topologySpreadConstraints`
  add?
- Explain why NimbusFS's rate limiter fails open by default and what the
  security trade-off is.
- Why does a `PodDisruptionBudget` protect against voluntary disruption
  but not involuntary disruption?

**Advanced**
- Cloud SQL regional HA promotes a standby in under a minute, but the
  application-visible outage is usually longer. Walk through every layer
  that contributes to the gap between "database is back" and "the app is
  serving correctly again."
- Why would routing reads to a Cloud SQL HA standby be unsafe without
  additional application changes, given this codebase's current
  `FileMetadata.version` semantics?
- NimbusFS's outbox pattern already tolerates a Pub/Sub outage. Explain
  precisely why bumping `outbox-publisher` to 2 replicas still improves
  availability rather than being redundant with that existing guarantee.

---

## 18. Phase 9 (HA half) completion checklist

- [x] Availability target chosen and justified (§2)
- [x] Multi-zone GKE architecture designed and manifests updated (§4)
- [x] Topology spread constraints added to API + all 4 workers
- [x] Cloud SQL HA design documented (§5)
- [x] Memorystore HA design documented (§7)
- [x] Redis failure handling confirmed IMPLEMENTED+TESTED (§8, pre-
      existing Phase 7 work)
- [x] Pub/Sub/worker resilience confirmed IMPLEMENTED+TESTED (§9,
      pre-existing Phase 8 work, +2 worker replica bumps this phase)
- [x] Failure matrix completed (§10)
- [x] Monitoring metric inventory + alerting design produced (§11–12)
- [x] Security-during-failover reviewed, no regression found (§13)
- [x] Cost comparison produced, no fabricated pricing (§14)
- [ ] Any claim above MEASURED against real GCP infrastructure — **not
      done this session**, see §16 for the exact procedure a future
      session with cluster/Cloud SQL/Memorystore access should run
