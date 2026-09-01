# NimbusFS — Failure Testing (Phase 9)

Companion to `docs/high-availability.md` and `docs/disaster-recovery.md`
— those documents make claims; this one is the procedure for converting
each claim from DESIGNED to MEASURED. Nothing in this file has been
executed in this session (no real GKE cluster, Cloud SQL, Memorystore,
or Pub/Sub was available) — every result field below is explicitly
**not filled in**, per the instruction never to fabricate a test result.

---

## 0. Environment labeling — read this before running anything

Every procedure below is tagged one of:

- **LOCAL/TEST** — runs against this repo's existing `pytest` suite and
  fakes (`tests/fakes/`). Safe to run anytime, by anyone, no
  infrastructure required. This is what CI (a future phase, README §23)
  would run on every PR.
- **STAGING** — requires a real but non-production GCP project/cluster/
  database dedicated to testing. Destructive by design (that is the
  point of a chaos test). Never point these at anything that also serves
  real users.
- **PRODUCTION** — read-only observation of a real incident, or a
  carefully scheduled, announced, rollback-planned drill during a
  maintenance window. **No procedure in this document should be run
  against production without an explicit go/no-go decision from
  whoever owns that environment's uptime.**

Getting this label wrong is the single most likely way this document
causes harm — an SRE skimming a chaos-testing doc under pressure and
running a `kubectl delete node` against the wrong cluster context is a
completely realistic failure mode this labeling exists to prevent.

---

## 1. Unit / Integration / Infrastructure / Failure tests — the distinction

This project's `pytest` suite already spans three of these four
categories; this phase does not blur them:

| Category | What it tests | Example in this repo | Needs real infra? |
|---|---|---|---|
| **Unit** | One function/class in isolation | `tests/test_events_envelope.py` | No |
| **Integration** | Multiple in-process components together, against fakes | `tests/test_events_integration.py`, `tests/test_reconciliation.py` | No — fakes stand in for GCS/Redis/Pub/Sub |
| **Infrastructure** | Does a manifest/config actually reconcile against a real control plane | `python -c "import yaml"` validation on `k8s/*.yaml` (Phase 5, still the only infra check that has ever run) | Partially — YAML validity, yes; real API-server acceptance, no |
| **Failure/Chaos** | Does the *running system* survive an *injected* real failure | Everything in §3–§12 below | **Yes, always** |

`pytest -q` (416 tests as of this phase) covers exactly the first two
rows. It is explicitly **not** a substitute for the fourth row, and this
document does not claim otherwise — see `docs/high-availability.md`'s
own caveat that Redis/Pub/Sub/worker resilience is "IMPLEMENTED +
TESTED" only against fakes, never "MEASURED."

---

## 2. Chaos testing scenarios

Numbered to match the user's own list. Each entry: label, prerequisite,
procedure, what "pass" looks like, what to record.

### 1. Delete a FastAPI Pod — **STAGING**
```bash
kubectl get pods -n nimbusfs -l app.kubernetes.io/component=api
kubectl delete pod <one-pod-name> -n nimbusfs
```
While running a concurrent load generator (reuse `scripts/load-test/
locustfile.py` or `k6-chunked-upload.js` against a simple GET), confirm
zero failed requests during the delete — this exact procedure is already
documented as Phase 5's self-healing demo (`k8s/README.md`); Phase 9
adds no new mechanism here, only re-lists it for completeness of this
consolidated chaos-testing document. **Record**: request error count
during the window, time for `kubectl get pods` to show 3/3 Ready again.

### 2. Kill a Kubernetes Node — **STAGING**
```bash
gcloud compute instances delete <node-vm-name> --zone=<zone>
```
Confirm: node transitions to `NotReady`, its Pods are rescheduled onto
other nodes (possibly after the default ~5 minute pod-eviction-timeout
if the node is merely unreachable rather than deleted — a genuinely
deleted VM is faster since the node object itself disappears), Service
endpoints exclude the dead Pods throughout. **Record**: time from node
deletion to all displaced Pods Running+Ready elsewhere.

### 3. Drain a Node — **STAGING**
```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
```
This is a *voluntary* disruption — confirm the PDB (`10-pdb.yaml`,
`minAvailable: 2`) is respected: `kubectl drain` should pause if evicting
the next Pod would violate it, not force through it. **Record**: whether
the drain completed without operator intervention, and how long it
paused (if at all) waiting for replacement Pods to become Ready.

### 4. Simulate zone failure — **STAGING**
Cordon+drain every node in one zone simultaneously (there is no
single "fail a zone" GCP button; this is the closest real approximation
short of an actual GCP-side outage):
```bash
for node in $(kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name); do
  kubectl cordon "$node"
done
kubectl drain <each node> --ignore-daemonsets --delete-emptydir-data
```
Confirm: remaining zones' Pods absorb all traffic, HPA scales up if
utilization crosses its threshold, no sustained error-rate increase.
**Record**: peak error rate (if any) and its duration during the
transition — this is the number `docs/high-availability.md` §16 asks
for and currently has none of.

### 5. Restart Redis — **STAGING**
```bash
gcloud redis instances failover <instance> --region=<region>
```
(or, for a self-managed test Redis in STAGING, a plain pod restart).
Confirm cache reads fall through to Postgres with elevated (not failed)
latency during the gap, locks fail safe (`ServiceUnavailableException`,
503 — never silently proceed unlocked), rate limiting behaves per
`RATE_LIMIT_FAIL_OPEN`. **Record**: failover duration, whether any
lock-guarded operation (e.g. a chunked upload completing) produced a
correctness violation (it must not, by design — HA doc §8).

### 6. Simulate Redis unavailability (no failover, just gone) — **STAGING**
Block Redis's port at the network layer (NetworkPolicy deny, or scale
the Redis proxy/instance to zero if using a test instance) rather than
failing it over, to test the *sustained outage* path distinctly from the
*failover* path in scenario 5. **Record**: does the API stay up
(expected: yes, degraded) or does anything unexpectedly hard-depend on
Redis (expected: no — Phase 7's invariant is "Redis owns nothing").

### 7. Trigger a database failover — **STAGING, schedule deliberately**
```bash
gcloud sql instances failover <instance>
```
This is the single most important number in `docs/high-availability.md`
§5 to actually measure — everything else in that section is reasoning
about *expected* behavior. **Record**: exact timestamp of command issued,
timestamp of first successful post-failover query from the application,
and the delta = the real Cloud SQL HA failover RTO contribution.

### 8. Stop a worker — **STAGING**
```bash
kubectl scale deployment nimbusfs-notification-worker -n nimbusfs --replicas=0
```
Confirm messages queue durably on the subscription (Pub/Sub backlog
metric rises, nothing is dropped), and processing resumes cleanly on
scale-up. **Record**: backlog depth at various durations, catch-up time
after scaling back to 2.

### 9. Kill a worker mid-message-processing — **STAGING**
```bash
kubectl delete pod <worker-pod> -n nimbusfs
```
issued while that worker is actively processing a large/slow message
(e.g. a large image for the thumbnail worker). Confirm: the message is
NOT acked before the kill, Pub/Sub redelivers after the ack deadline, and
the eventual successful processing produces exactly one thumbnail/
notification/metadata update — **no duplicate**. This is
`tests/test_base_worker.py`'s LOCAL/TEST assertion made real; the LOCAL
version already passes (416/416), this scenario is what proves it also
holds against genuine Pub/Sub redelivery semantics rather than the fake
broker's approximation of them (`tests/fakes/fake_pubsub.py`'s own
docstring notes it does not simulate real ack-deadline/redelivery
timing).

### 10. Simulate GCS failure — **STAGING**
Hardest to inject faithfully against real GCS (no widely-available
"break GCS for me" API) — the practical approximation is a NetworkPolicy
egress block toward `storage.googleapis.com`'s IP ranges for a single
Pod, or IAM-revoking that Pod's GSA temporarily. Confirm: uploads/
downloads fail with the correct typed exception
(`StorageException` subclasses), `retry_async`/`CircuitBreaker` behave
as designed (README §11/§13), and the failure never corrupts a
`FileMetadata` row (Phase 3's rollback guarantee).

### 11. Simulate Pub/Sub failure — **STAGING**
Same practical approach as #10 (network/IAM block), or use the Pub/Sub
emulator's own fault-injection if using it for this test instead of real
Pub/Sub. Confirm: outbox rows accumulate `PENDING`, no event is lost,
publisher resumes and drains the backlog once connectivity returns.

### 12. Restore from database backup — **STAGING**, full procedure in
`docs/backup-restore.md` §"Restore Procedure" rather than duplicated
here.

### 13. Restore application in secondary region — **STAGING**, full
procedure in `docs/disaster-recovery.md` §9 "Failover runbook."

---

## 3. RTO/RPO measurement methodology

Do not assert compliance with the targets in `docs/disaster-recovery.md`
§1 — **measure** the actual values from a real drill and compare:

```
Failure begins:      <timestamp T0, from the chaos-injection command>
Detection:            <timestamp T1, from the first alert/health-check failure>
Recovery starts:      <timestamp T2, from the first remediation action>
Service restored:     <timestamp T3, from the first successful post-recovery
                        request/health check>
Last durable write
before failure:        <timestamp T_data, from the database/outbox state>
First write after
recovery reflects
pre-failure state up
to:                    <timestamp T_recovered_data, from restore verification>

RTO (measured)  = T3 - T0
RPO (measured)  = T_data - T_recovered_data
```

Example of the *shape* of a completed record (**illustrative format
only — not a real drill result**):
```
Failure begins:    10:00:00
Service restored:  10:23:14
RTO (measured):    23m 14s
```

Every field above is currently **unfilled** for NimbusFS — this
methodology exists so that the *next* session with real infrastructure
access has a template to fill in, not so this document can claim a
number it never produced.

---

## 4. Backup/restore test — see `docs/backup-restore.md`

The full 7-step exercise (create test data → backup → modify/delete →
restore → verify → measure → document) lives there in full, with
runnable `gcloud`/`psql` commands, to avoid the same content existing in
two places and drifting.

---

## 5. What LOCAL/TEST already covers (no infra needed)

For completeness, the failure-adjacent behavior this repo's `pytest`
suite already verifies without any of the above — useful as a fast
regression check before ever touching STAGING:

| Behavior | Test file |
|---|---|
| DB/Redis/Storage retry-with-backoff | `tests/test_distributed.py` |
| Circuit breaker open/half-open/closed | `tests/test_distributed.py` |
| Cache degradation on every Redis op | `tests/test_caching.py` |
| Distributed lock failure-safety | `tests/test_caching.py`, `tests/test_distributed.py` |
| Rate limiter fail-open vs fail-closed | `tests/test_rate_limiting.py` |
| Outbox row survives publish failure | `tests/test_outbox_publisher_worker.py` |
| Worker ack/nack/duplicate-delivery handling | `tests/test_base_worker.py` |
| End-to-end event chain incl. a mid-flight Pub/Sub outage | `tests/test_events_integration.py` |
| Postgres/GCS drift detection (read-only) | `tests/test_reconciliation.py` (Phase 9) |
| `/health`/`/ready`/`/live` shape and behavior | `tests/test_health.py` |

Run with: `pytest -v` (no environment variables or services beyond
Python's own dependencies required — see `tests/conftest.py`).

## 6. Phase 9 (failure-testing half) completion checklist

- [x] Chaos scenarios 1–13 documented with commands and pass criteria
- [x] Environment labeling scheme defined (§0)
- [x] Unit/Integration/Infrastructure/Failure test categories
      distinguished with real examples from this repo (§1)
- [x] RTO/RPO measurement methodology and record template provided (§3)
- [x] Existing LOCAL/TEST coverage catalogued (§5)
- [ ] Any scenario actually executed against STAGING/PRODUCTION — **not
      done this session**
