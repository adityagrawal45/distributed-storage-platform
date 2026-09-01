# NimbusFS — Disaster Recovery (Phase 9)

Companion to `docs/high-availability.md` (§1 there explains the HA/DR
split — read that first if you haven't). This document covers what
happens when a failure is bigger than HA can absorb: an entire GCP
region, a corrupted/mass-deleted dataset, or a catastrophic operator
error.

Same labeling discipline as the HA doc: **DESIGNED / IMPLEMENTED /
TESTED / MEASURED**. Nothing in this document is MEASURED — no real
region failover, no real restore, was performed this session (no
infrastructure available). See §14 for exactly what a future session
with real GCP access should run to convert these targets into measured
facts.

---

## 1. RTO and RPO

**RTO (Recovery Time Objective): < 4 hours**, for a full regional
failover (the worst case this document covers). **RPO (Recovery Point
Objective): < 1 hour**, bounded by Cloud SQL's continuous transaction-log
backup / PITR granularity.

Why these values, not the tighter "<1 hour RTO / <15 min RPO" a more
aggressive DR posture might target:

- **RPO < 1 hour** is achievable with **PITR alone** (§1.5 of the
  high-availability doc; `enable_point_in_time_recovery: true`), which
  this design already calls for — Cloud SQL retains a continuous
  transaction log, so a restore can target "as close to the failure as
  the log survived," typically seconds, not the full hour. The 1-hour
  figure is a *conservative ceiling* that accounts for a scenario where
  the transaction log itself was affected by whatever caused the
  disaster (e.g. the primary region's storage layer, not just the
  compute) and the restore has to fall back to the most recent automated
  daily backup plus whatever WAL segments made it to a **different**
  region before the failure — see §5 for why cross-region backup
  location matters here.
- **RTO < 4 hours** reflects a genuinely **manual, warm-standby
  active-passive** failover (§8 below), not an automated one: standing up
  a secondary region's environment from Secret Manager + Terraform-free
  manifests (this phase's manifests are plain YAML, not templated per-
  environment — see README §12's Kustomize/Helm trade-off note),
  restoring the database into the standby region, repointing DNS, and
  verifying. A human executes a documented runbook (§9); none of this is
  automatic failover. Claiming < 1 hour for a process that involves a
  human reading a runbook and running `gcloud` commands under incident
  pressure would not be a target this architecture backs — see the
  Critical Rule at the top of this whole exercise.
- Both numbers are **targets a future MEASURED drill must validate**, not
  numbers derived from an actual timed rehearsal (§14). If a real drill
  comes in worse than these targets, the honest next step is to widen the
  target or invest in tightening the runbook — not to keep publishing an
  unverified number.

---

## 2. GCS durability strategy

GCS's own object durability (11 nines, annual expected object loss —
Google's published figure, not this project's) is **already extremely
high regardless of bucket location type**; the decision below is about
**availability during a regional outage**, not object durability.

**Recommendation: keep the existing regional bucket as primary, and add
Turbo/dual-region replication is NOT recommended for NimbusFS today.**

Reasoning:
- A **regional bucket** (current state, Phase 3) has the lowest cost and
  lowest write latency (co-located with the GKE cluster and Cloud SQL),
  at the cost of GCS-level unavailability if the *bucket's specific
  region* has an outage — a genuinely rare, distinct event from "a zone
  in that region is down" (§10 in the HA doc already covers zone-level;
  this is region-level).
- A **dual-region bucket** gives synchronous-or-near-synchronous
  replication between two specific regions with automatic failover
  reads, at roughly the cost premium noted in the HA doc's cost table
  and materially higher write latency (every write must satisfy both
  regions before being durable, though GCS's actual dual-region
  implementation uses turbo replication with an RPO measured in minutes
  for the async path).
- A **multi-region bucket** (broadest, most expensive, GCS chooses the
  serving region) is overkill for a single-region application — it
  optimizes for *global read locality*, which is not this system's
  problem (all NimbusFS API traffic originates from one GKE region).

**The actual recommendation for NimbusFS's DR posture**: keep the primary
bucket regional (cost-optimal for normal operation), and use **GCS's
Storage Transfer Service or a scheduled `gsutil rsync`/bucket
notification-driven copy** to replicate objects into a *second, regional*
bucket in the DR region, on a schedule consistent with the RPO in §1
(replication lag becomes part of the RPO budget, so it must run at least
hourly to meet a <1h RPO on file bytes, mirroring the database PITR
target). This is cheaper than a dual-region bucket for a mostly-append-
only, rarely-overwritten workload (files are versioned, not overwritten
in place — README §3.4) and gives an explicit, auditable replication
job rather than an opaque managed-replication SLA.

**This phase does not implement that replication job** — it is designed
and recommended here, matching the user's explicit instruction not to
"blindly choose the most expensive option," and left as a scoped future
addition alongside the equally-undone stuck-`COMPLETING` reconciliation
job (README §23).

**Status: DESIGNED (recommendation), NOT IMPLEMENTED.**

---

## 3. GCS object protection

- **Object versioning**: recommend **enabling GCS Object Versioning** on
  the production bucket. This is independent of and complementary to
  NimbusFS's own application-level `FileVersion` table (Phase 2) — the
  application's versioning tracks *user-intended* versions (explicit
  replace/upload-new-version actions); GCS Object Versioning is a safety
  net against *accidental or malicious* object mutation/deletion at the
  storage layer itself (a bug in `StorageService.delete`, a
  misconfigured lifecycle rule, a compromised credential issuing
  `gsutil rm`) that the application layer would never see coming.
- **Lifecycle policy**: with versioning on, add a lifecycle rule that
  deletes *noncurrent* versions after a bounded retention (e.g. 30 days)
  — otherwise every replace/delete permanently grows storage cost.
  Current versions are never touched by a lifecycle rule; only prior
  versions age out.
- **Soft delete** (GCS's own bucket-level soft-delete feature, distinct
  from NimbusFS's application-level `is_deleted` soft-delete on
  `FileMetadata`): recommend enabling with a short retention (e.g. 7
  days) as a second, storage-layer safety net specifically against
  accidental hard deletes issued outside the application (direct
  `gsutil`/console access) — the one class of deletion NimbusFS's own
  `/files/{id}/permanent` guardrails (README §10: only removes bytes if
  no other row references the object) cannot protect against, because it
  bypasses the application entirely.
- **Protection against accidental deletion, application-side**:
  unchanged from Phase 3 — permanent delete already requires a prior
  soft-delete and checks `object_name_in_use` before touching bytes.
  Phase 9 adds no new application code here; it adds the storage-layer
  nets above this paragraph precisely because the storage layer is the
  one path the application-side guardrails cannot see.

**Status: DESIGNED (recommendation). NOT IMPLEMENTED** — enabling
versioning/lifecycle/soft-delete on the bucket is a `gsutil`/console
configuration action against a real bucket, not application code; no
real bucket exists in this session to apply it to.

---

## 4. Data consistency across systems

| System pair | Consistency model | Why |
|---|---|---|
| Postgres row ↔ GCS object (upload) | Strong, by construction | Phase 3's rollback-on-failure + Phase 6's Compose-then-verify-then-create-row ordering mean a `FileMetadata` row is only ever created after its bytes are confirmed present |
| Postgres row ↔ Outbox event | Strong (same transaction) | Phase 8's whole point — README §15 |
| Outbox event ↔ Pub/Sub message | Eventual, at-least-once | Bounded by outbox-publisher poll interval; never lost, may be delayed (HA doc §9) |
| Pub/Sub message ↔ Consumer side effect (thumbnail, notification) | Eventual, effectively-once | `ProcessedEvent` dedup (Phase 8); may lag the triggering upload by however long the queue + processing takes |
| Postgres ↔ Redis cache | Eventual, TTL-bounded | Phase 7's governing invariant — Redis is disposable, never authoritative (README §14) |
| Postgres row ↔ GCS object (post-write drift) | **Assumed strong, occasionally violated** | This is the gap §5 (reconciliation) exists for — a row and its object are only guaranteed consistent *at the moment of upload*; nothing re-verifies that guarantee afterward except reconciliation |

The last row is the one this phase adds new machinery for. Every other
row was already true given Phases 1–8; Phase 9's contribution to data
consistency is entirely in acknowledging and checking the one direction
that was previously "assumed, never verified."

---

## 5. Reconciliation

### 5.1 The problem

Postgres and GCS can drift even when every individual code path is
correct: a process killed at exactly the wrong instant outside any
transaction boundary, a manual `gsutil rm` against the bucket, a botched
migration, a restore that brought Postgres back to an earlier point than
GCS (or vice versa — see §14's restore-then-reconcile step). Two
directions are possible:

- **`FileMetadata` says a file exists; GCS says the object is missing.**
  Dangerous: a user sees a file in their listing that 404s on download.
- **GCS contains an object; no `FileMetadata` row references it.**
  Costly, not dangerous: wasted storage, invisible to users (nothing in
  the UI points at it).

### 5.2 What this phase implements

`app/services/reconciliation_service.py` + `app/workers/
reconciliation_job.py` (run as `k8s/22-cronjob-reconciliation.yaml`,
every 6 hours) detect **only the first, dangerous direction**
(`METADATA_WITHOUT_OBJECT`). For every non-deleted, `upload_status=
COMPLETED` `FileMetadata` row (walked via keyset pagination —
`FileMetadataRepository.list_completed_batch`, never `OFFSET`, so the
job stays bounded in memory and query cost regardless of table size), it
confirms the object exists in GCS via `StorageService.get_blob_metadata`
and records a `ReconciliationIssue` on `StorageObjectNotFoundException`.

The **second direction (orphaned objects) is explicitly out of scope
this phase** — detecting it requires listing the entire bucket
(`list_blobs`), which is a real, supported GCS operation this codebase
has simply never needed before and the test fakes don't model. It is the
lower-risk half of the problem (money, not correctness) and is left as a
clearly-named future addition rather than silently unhandled — see the
service's module docstring.

### 5.3 Safety guarantees

- **The service has no delete, update, or write code path anywhere in
  its call graph.** Not "delete gated behind a flag" — no delete
  statement exists at all. `tests/test_reconciliation.py::
  test_never_mutates_or_deletes_anything` asserts the flagged row is
  byte-for-byte unchanged after a run that found it.
- **A GCS error that is not "object not found" aborts the run rather
  than being recorded as a false-positive issue** — a timeout or
  permission error must not be indistinguishable from a genuinely
  missing object in the report a human reads afterward.
- **`RECONCILIATION_MAX_ISSUES` bounds worst-case GCS API calls** — a
  badly corrupted dataset triggers a truncated, clearly-marked partial
  report (`ReconciliationReport.truncated`) rather than an unbounded
  scan.
- **The Job's exit code is the alerting hook**: `0` clean, `1` issues
  found, `2` the scan itself couldn't complete — see `reconciliation_job.
  py`'s docstring.

### 5.4 What a human does with a finding, today

Nothing automatic. A `METADATA_WITHOUT_OBJECT` finding is logged
(structured, `reconciliation_issue_found`) and reflected in the Job's
exit code/failure count for whatever alerting is wired to
`kubectl get jobs` (§ monitoring, HA doc §11). The **operator** decides
per-row remediation — mark the row `FAILED` and notify the user, attempt
re-upload from a client-side cache if one exists, or investigate further
— because every option risks being wrong for some subset of cases (e.g.
if the underlying cause was a *false positive* from a transient GCS
issue that should have aborted the run per §5.3 but didn't for some
unforeseen reason). A future phase automating remediation is explicitly
a **separate, reviewed change**, per the user's own instruction not to
delete data during reconciliation without explicit safeguards.

**Status: IMPLEMENTED + TESTED** (6 tests, `tests/test_reconciliation.
py`, against `FakeGCSClient`). **Not MEASURED** against a real bucket
with real drift.

---

## 6. Multi-region DR design

**Selected model: Active-Passive, Warm Standby.**

```
              PRIMARY (Region A)                    SECONDARY (Region B)
        +---------------------------+          +---------------------------+
        |  GKE regional cluster     |          |  GKE regional cluster     |
        |  (multi-zone, HA doc)     |          |  (multi-zone, minimal     |
        |  full replica count       |          |  replica count — "warm")  |
        |                           |          |                           |
        |  Cloud SQL (regional HA)  |  backup  |  Cloud SQL instance,      |
        |  ------------------------ | -------> |  created FROM a           |
        |                           | replicate|  cross-region backup      |
        |  GCS regional bucket      |  (§2)    |  GCS regional bucket      |
        |  (primary)                | -------> |  (DR copy)                |
        +---------------------------+          +---------------------------+
                    ^                                       ^
                    |                                       |
                    +------------- DNS / Global LB ---------+
                         (failover target, §7 — normally
                          points at Region A only)
```

**Why Active-Passive Warm Standby, not the alternatives**:

| Option | RTO | Cost | Complexity | Verdict |
|---|---|---|---|---|
| **Cold standby** (infra defined, nothing running) | Hours-to-a-day (provision everything from scratch) | Lowest | Lowest | RTO too slow for a <4h target once you account for Cloud SQL instance creation + restore time on top of provisioning |
| **Warm standby (chosen)** | Within the <4h target — GKE cluster and a minimal-replica-count Deployment already exist; the slow parts are DB restore + traffic cutover | Moderate — a running (small) GKE node pool + a Cloud SQL instance in region B, most of the time doing nothing | Moderate | Right balance for this project's stated RTO/RPO and the user's instruction not to default to active-active |
| **Active-active** | Near-zero | Highest — full duplicate capacity, and a hard multi-writer-database problem this codebase has no answer for (Postgres is not natively multi-region-writable without a very different architecture — Spanner, CockroachDB, or an application-level conflict-resolution scheme) | Highest | **Explicitly rejected.** No requirement in this project justifies solving multi-writer consistency; doing so "because it sounds enterprise-grade" is exactly what this phase's instructions warn against |

**Data synchronization to the standby**:
- Database: Cloud SQL cross-region **automated backups** (§ backup-
  restore.md) copied to a bucket in region B, restorable into a new
  Cloud SQL instance in region B on demand — not a continuously-running
  replica (that would blur into active-active's cost/complexity without
  the RTO win, since a continuously-replicated standby still needs
  promotion logic and doesn't reduce restore time to zero the way, say,
  Cloud SQL's *same-region* HA standby does).
- Storage: the regional-to-regional object copy job from §2.
- Configuration/secrets: §7 below.

**Status: DESIGNED. NOT IMPLEMENTED** — no region-B environment,
Cloud SQL instance, or bucket exists in this session.

---

## 7. Secrets and IAM for DR

- **Google Secret Manager**, not Kubernetes `Secret` objects checked into
  anything, and never a copied service-account JSON key file between
  regions — Workload Identity (already the pattern since Phase 5) means
  the *same* GSA-to-KSA binding mechanism works in region B once its KSA
  exists there; no credential material needs to physically travel
  between regions at all. This is a real security property of the
  existing architecture, not new Phase 9 work — restated here because DR
  is exactly the scenario where the temptation to "just copy the key
  file over quickly" is highest under incident pressure, and the
  runbook (§9) should say explicitly not to.
- **IAM**: each of the five now-existing service accounts (API + 4
  workers, see `16-worker-serviceaccounts.yaml`) needs its region-B
  counterpart GSA provisioned with the *same* roles, ahead of any actual
  failover — provisioning IAM during an incident is exactly the kind of
  step that turns a 30-minute failover into a multi-hour one. This is a
  standing region-B setup task, not a failover-time task.
- **Least privilege carries over unchanged**: the reconciliation job's
  read-only GSA (§ this phase's k8s additions) needs a region-B
  equivalent with the same read-only scope — DR is not a reason to grant
  broader access "just in case," per this document's own security
  principle (HA doc §13).

**Status: DESIGNED.**

---

## 8. Global traffic failover

NimbusFS's existing GKE Ingress (Phase 5, `15-ingress.yaml`) already
uses a **Global External Application Load Balancer** — this is not new
infrastructure, but it is the mechanism region-level failover reuses:

- **Normal operation**: the GCLB's backend service points at Region A's
  NEGs only (single-region deployment, HA doc §3).
- **Regional failover**: add Region B's backend service (once the warm
  standby is promoted per the runbook, §9) to the same GCLB, or repoint
  a **health-checked** backend configuration — a global external ALB can
  route by backend health across regions natively, which is the whole
  reason to use the global (not regional) product tier here, even for a
  currently-single-region deployment: it costs nothing extra to keep
  that door open.
- **This phase does not pre-provision Region B's backend/NEGs** —
  standing up an idle, fully-wired second region's load-balancer backend
  before it's needed would itself be the "unnecessary complexity" the
  prompt warns against; the runbook (§9) includes the step to add it
  *during* an actual failover, which is the honest cost of the
  warm-standby (not active-active) choice.

**Status: DESIGNED.**

---

## 9. Failover runbook (manual, warm standby → active)

**Label: STAGING/PRODUCTION-scale procedure — never rehearse this
against a real production primary without an explicit maintenance
window; rehearse in a dedicated STAGING project first (see failure-
testing.md §"Environment labeling").**

1. **Declare the incident.** Confirm Region A is genuinely unavailable
   (not a transient GCLB health-check flap) — this determination is a
   human judgment call, not automatable in this design.
2. **Restore the database in Region B** from the most recent cross-
   region-replicated backup (`docs/backup-restore.md` §"Restore
   Procedure") — this is the single longest step and the primary driver
   of the RTO budget in §1.
3. **Verify Secret Manager entries exist in Region B** (§7) — if any were
   missed during standing setup, create them now; this should be a rare,
   fallback step, not the expected path.
4. **Scale up Region B's GKE Deployments** (already defined via the same
   manifests as Region A — no new YAML needed, they are region-agnostic)
   from warm-standby minimal replica count to full serving capacity.
5. **Point Region B's app at the restored Cloud SQL instance and the DR
   copy of the GCS bucket** — both already the target of Region B's
   `ConfigMap`/`Secret` values if provisioned correctly ahead of time
   (§7); verify, don't assume, under incident pressure.
6. **Run startup verification** — `/health` and `/ready` (Phase 4) on a
   handful of Region B Pods before adding them to the load balancer;
   these endpoints already check real DB/Redis/Storage connectivity,
   which makes them the right smoke test here without inventing a new
   one.
7. **Add Region B to the Global external ALB's backend configuration**
   (§8) and/or update DNS (§10) to shift traffic.
8. **Monitor error rates and the reconciliation job's next scheduled run**
   in Region B — a fresh reconciliation pass after a database restore is
   specifically useful here, since a restore-to-an-earlier-point can
   reintroduce exactly the `METADATA_WITHOUT_OBJECT` drift §5 detects
   (rows resurrected by the restore that point at objects since deleted,
   or the reverse).
9. **Declare recovery, record the actual timestamps** — this is the raw
   material for §14's RTO/RPO measurement, and for updating this
   document's targets if reality disagrees with them.

**Status: DESIGNED (documented procedure). NOT TESTED end-to-end.**

---

## 10. DNS failover

- **TTL**: recommend a **low TTL (60–300s)** on the production DNS record
  well *before* any incident — a record's existing TTL at the moment of
  an outage governs how fast clients honor a change, and it cannot be
  lowered retroactively during the incident itself (any client that
  already cached the old TTL will hold onto the old answer for its
  remaining duration regardless of the zone file being updated). This is
  a standing configuration task, not a failover-time task, and worth
  stating plainly since it's an easy step to forget until it's too late.
- **DNS changes are not instantaneous even at a low TTL** — recursive
  resolvers, corporate DNS caches, and some clients' own OS-level caching
  can hold an answer longer than the TTL nominally allows. Budget for
  this in the RTO: the "traffic has actually shifted" milestone lags
  "the DNS record was updated" by an unpredictable, sometimes multi-
  minute tail even with a well-configured TTL.
- **If using a single global external ALB IP for both regions** (§8's
  backend-health-based routing), DNS itself never needs to change during
  failover — this is the preferred design specifically because it
  removes DNS TTL/caching uncertainty from the RTO calculation entirely.
  A DNS-based failover (separate IPs/records per region, switched via a
  health-checked DNS policy) is the fallback if a single global LB
  frontend isn't feasible for some future reason, and inherits every
  caveat in this section.

**Status: DESIGNED.**

---

## 11. Design decisions

- **Active-passive warm standby over cold or active-active** — §6's
  table is the full reasoning; restated as a decision because it is the
  single highest-leverage choice in this document.
- **Reconciliation is read-only and single-direction this phase** — §5.2/
  5.3; the alternative (build both directions, or build auto-remediation)
  was rejected as scope beyond what a first, safe reconciliation pass
  should attempt, per the user's own explicit "do not automatically
  delete data" instruction.
- **Object replication to a second regional bucket, not a dual-region
  bucket** — §2; chosen for cost and explicit auditability over an
  opaque managed SLA, and left undesigned-in-code (documented
  recommendation only) since it has no dependency the rest of Phase 9
  needs to build around.
- **A single global external ALB frontend, not per-region DNS records**
  — §10; removes DNS propagation delay from the RTO critical path
  entirely, which is a strictly better trade for a two-region
  active-passive design than it would be worth the complexity for.
- **RTO/RPO set as explained targets, not backed by a rehearsed drill**
  — consistent with the Critical Rule: a number this session cannot
  measure is stated as a target with its derivation shown, not asserted
  as a compliance fact.

---

## 12. Failure scenarios (region-level, narrated)

**Scenario: Region A becomes fully unreachable (major outage).**
Detection: GCLB backend health checks for every Region-A NEG fail
simultaneously — this looks categorically different from a single-zone
failure (HA doc §10), which only ever fails a subset of backends.
Impact: total outage until the runbook (§9) completes. Recovery: manual,
per §9, target <4h RTO, up to ~1h of data loss bounded by PITR/backup
replication cadence (§1). What's never lost: anything already durably
committed to Cloud SQL and replicated per §2/§6 before the outage began.

**Scenario: A mass-deletion incident (compromised credential, application
bug) deletes many `FileMetadata` rows or GCS objects.**
This is NOT a regional outage, but sits in this document rather than the
HA doc because the recovery mechanism is DR's, not HA's: point-in-time
restore (`docs/backup-restore.md`) to just before the deletion, then run
reconciliation (§5) against the restored state to catch any GCS-side
drift the restore didn't also fix (e.g. if objects were deleted directly
from GCS rather than via the application, a Postgres-only restore
resurrects rows pointing at objects that are still gone — exactly the
`METADATA_WITHOUT_OBJECT` case §5 exists to surface).

---

## 13. Interview questions

**Beginner**
- What is the difference between RTO and RPO?
- Why can't GCS's 11-nines durability figure alone be used to claim "we
  never need backups"?

**Intermediate**
- Why is Cloud SQL's regional HA standby not a substitute for
  cross-region disaster recovery?
- Explain the two directions a Postgres/GCS consistency check can find
  drift, and why NimbusFS's Phase 9 reconciliation job only implements
  one of them.
- Why does a low DNS TTL configured *during* an incident not help, and
  what should have been done instead?

**Advanced**
- Design the failure mode where restoring Postgres to a point-in-time
  makes the system's GCS state *more* inconsistent, not less, and explain
  how reconciliation is supposed to catch it.
- Why was active-active explicitly rejected for NimbusFS given its
  current PostgreSQL-based architecture, and what would have to change
  architecturally to make active-active viable?
- The runbook in §9 restores the database before scaling up compute in
  the DR region. Argue for and against reversing that order.

---

## 14. How to actually verify this document's claims

1. **STAGING**: create test data, take a backup, delete/modify the data,
   restore, verify — full procedure and commands in `docs/backup-
   restore.md`. Record the actual wall-clock time as the RPO/RTO
   component attributable to database recovery.
2. **STAGING**: stand up a genuine second-region environment (even a
   minimal one) and execute the §9 runbook against it end to end,
   timestamping each numbered step. This is the only way to convert §1's
   RTO target from DESIGNED to MEASURED.
3. **STAGING**: after a restore in step 1, run `python -m app.workers.
   reconciliation_job` against the restored database and confirm it
   correctly surfaces any injected drift (e.g. manually delete one GCS
   object the restored database still references, confirm the job
   reports exactly that file).
4. **PRODUCTION**: none of the above should be run against production
   without an explicit, scheduled maintenance window and a rollback
   plan for the drill itself — see `docs/failure-testing.md`'s
   environment-labeling section.

## 15. Phase 9 (DR half) completion checklist

- [x] RTO/RPO targets defined and justified (§1)
- [x] GCS durability/protection strategy analyzed, recommendation given
      (§2–3)
- [x] Data consistency across all 5 systems analyzed (§4)
- [x] Reconciliation designed, implemented, and tested for the
      dangerous direction (§5)
- [x] Multi-region DR model selected with alternatives compared (§6)
- [x] Secrets/IAM DR posture documented, no key-copying (§7)
- [x] Global traffic failover mechanism identified (§8)
- [x] Manual failover runbook written (§9)
- [x] DNS failover strategy documented with realistic caveats (§10)
- [ ] Any RTO/RPO number MEASURED via a real drill — **not done this
      session**, see §14
