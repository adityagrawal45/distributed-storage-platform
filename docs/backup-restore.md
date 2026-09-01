# NimbusFS — Backup & Restore (Phase 9)

Companion to `docs/disaster-recovery.md` (uses this document's procedure
in its §9 failover runbook and §1 RPO derivation) and `docs/failure-
testing.md` (scenario #12 points here). Covers **Cloud SQL** backup/PITR
in depth — GCS's own protection mechanisms (versioning/lifecycle) are in
`docs/disaster-recovery.md` §3, since they're a different system with a
different failure mode, and duplicating them here would be exactly the
kind of interleaving `docs/high-availability.md` §1 warns against.

**A backup that has never been restored is not proven reliable** — this
is the premise for §3's exercise existing as an executable procedure,
not a checkbox. As of this Phase 9 session, that exercise has **not been
run** (no real Cloud SQL instance available) — see §4 for exactly what
that means for this document's credibility and what to do about it.

---

## 1. Backup strategy

| Setting | Value | Reasoning |
|---|---|---|
| Automated backups | Enabled, daily | Baseline Cloud SQL feature, no reason not to |
| Backup window | Off-peak for the deployment's primary user timezone | Backups briefly increase I/O load; avoid stacking that on peak traffic |
| Retention | **14 days** | Long enough to catch a slow-discovered corruption (a bug shipped and not noticed for a week is a realistic scenario) without the storage cost of, say, 90-day retention that this project's current scale doesn't obviously need. Revisit if compliance requirements (none stated for NimbusFS today) demand longer. |
| Point-in-time recovery (PITR) | **Enabled** (`enable_point_in_time_recovery: true`), binary logging on | This is what makes the RPO in `docs/disaster-recovery.md` §1 (<1h, often much better) achievable — a daily-backup-only strategy without PITR would have an RPO as bad as "up to 24 hours," which this project explicitly does not accept |
| Backup location | A **separate region** from the primary, or Cloud SQL's cross-region backup replication if using an edition that supports it | A backup stored only in the same region as the primary does not survive the regional-outage scenario `docs/disaster-recovery.md` §6 is designed for — a backup co-located with the disaster it's meant to protect against is not a disaster recovery mechanism |
| Backup verification | Automated restore-test on a schedule (§3, not yet automated — see §5) | An unverified backup is a belief, not a fact — this document's own opening line |

**Provisioning commands** (illustrative — run against a real project,
never assume these are pre-applied):

```bash
# Create a regional (HA) instance with backups + PITR enabled from the start.
gcloud sql instances create nimbusfs-db \
  --database-version=POSTGRES_16 \
  --region=<region> \
  --availability-type=REGIONAL \
  --backup-start-time=03:00 \
  --enable-bin-log \
  --retained-backups-count=14 \
  --retained-transaction-log-days=7

# If retrofitting onto an existing instance instead:
gcloud sql instances patch nimbusfs-db \
  --backup-start-time=03:00 \
  --enable-bin-log \
  --retained-backups-count=14 \
  --retained-transaction-log-days=7
```

**Status: DESIGNED, documented.** No real Cloud SQL instance exists in
this session to run these against.

---

## 2. Point-in-time recovery, conceptually

PITR restores to any second within the retained transaction-log window
(`--retained-transaction-log-days` above), not just to a daily backup
boundary. Practically:

```
Daily backup: 03:00 ────────────────────────────► 03:00 (next day)
                                    |
                          Incident at 14:32:07
                                    |
                          PITR target: 14:32:00
                          (a few seconds before the
                           incident, chosen deliberately
                           short of the exact moment if the
                           incident's cause — e.g. a bad
                           migration — has a known start time)
```

Restoring via PITR **always creates a new Cloud SQL instance** — it does
not restore in place onto the existing one. This is deliberate on
Google's part (an in-place restore would destroy the ability to compare
before/after, or to abandon a restore attempt) and matters operationally:
the application's `POSTGRES_HOST` must be repointed at the new instance's
connection name as part of any restore, which is exactly what the
runbook below spells out rather than leaving implicit.

---

## 3. Restore exercise — executable procedure

**Label: STAGING only.** Never run step 3 (delete/modify) against
anything serving real traffic.

### Step 1 — Create test data
```bash
# Against a STAGING NimbusFS deployment, via its own API:
curl -X POST https://staging.nimbusfs.example/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"restore-test@nimbusfs.io","password":"...","first_name":"Restore","last_name":"Test"}'

# ...log in, then create a marker file whose content you can verify later:
echo "restore-test-marker-$(date +%s)" > /tmp/marker.txt
curl -X POST https://staging.nimbusfs.example/api/v1/files/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/marker.txt"
# Record the returned file_id and the marker content.
```

### Step 2 — Take a backup
```bash
gcloud sql backups create --instance=nimbusfs-db-staging
# Record the backup ID from the output.
BACKUP_ID=<from output>
```

### Step 3 — Modify/delete the test data
```bash
curl -X DELETE https://staging.nimbusfs.example/api/v1/files/<file_id>/permanent \
  -H "Authorization: Bearer $TOKEN"
# (Requires a prior soft-delete via DELETE /metadata/{id} first, per README §10 —
# this is intentionally the real, guarded deletion path, not a raw DB DELETE,
# so the test exercises the same code path a real accidental-deletion incident would.)
```

### Step 4 — Restore
```bash
# PITR restores into a NEW instance:
gcloud sql instances clone nimbusfs-db-staging nimbusfs-db-staging-restore-test \
  --point-in-time="<timestamp just before step 3>"

# Or restore from the specific backup taken in step 2:
gcloud sql backups restore $BACKUP_ID \
  --restore-instance=nimbusfs-db-staging-restore-test \
  --backup-instance=nimbusfs-db-staging
```

### Step 5 — Verify
```bash
# Point a throwaway app instance's POSTGRES_HOST at
# nimbusfs-db-staging-restore-test's connection name, then:
curl https://<throwaway-instance>/api/v1/files/<file_id> -H "Authorization: Bearer $TOKEN"
# Confirm the marker file's metadata is present again (restored from
# before the deletion) — and separately confirm whether its GCS object
# still exists (permanent-delete removed bytes in step 3, so it should
# NOT — this is exactly the METADATA_WITHOUT_OBJECT drift scenario
# docs/disaster-recovery.md §14 step 3 asks you to feed into the
# reconciliation job as a real test of it catching real drift):
python -m app.workers.reconciliation_job  # against the restored instance
# Expect exit code 1, with exactly one issue for this file_id.
```

### Step 6 — Measure recovery time
Record: timestamp backup/PITR restore command issued → timestamp the
verification query in Step 5 succeeded. This is the "database recovery"
component of the RTO measurement template in `docs/failure-testing.md`
§3 — it is not the *whole* application RTO (that also includes
repointing config, redeploying, etc., per `docs/disaster-recovery.md`
§9), but it is the single largest and most measurable piece of it.

### Step 7 — Document the result
Fill in and keep, per drill:
```
Date:                    <date>
Environment:             STAGING
Backup type:             [scheduled daily | on-demand | PITR]
Data verified present:   [yes/no — the marker file's metadata]
Data verified consistent
with GCS (reconciliation):[clean | N issues found — see job output]
Restore duration:        <measured, Step 6>
Issues encountered:      <any surprises — write these down, they are
                          more valuable than the happy-path timing>
```

**Nothing above has been executed in this session** — no STAGING Cloud
SQL instance was available. This procedure is the artifact this phase
delivers in place of a result; running it and filling in §3's template
is the very next action item for a session with real GCP access.

---

## 4. Why this document does not contain a filled-in result

Per this phase's explicit instruction: never fabricate a recovery time,
availability number, or test result. A plausible-looking "Restore
duration: 8m 42s" written without ever running the commands above would
be indistinguishable, to a future reader, from a real measurement — and
strictly worse than an honest blank, because it would be *trusted*. The
blank template in §3 Step 7 is the correct state of this document until
someone actually runs it.

---

## 5. Toward automation (future, not this phase)

A scheduled STAGING restore-test (§3, run weekly via Cloud Scheduler +
a Cloud Build/Cloud Function trigger, tearing down the restored instance
afterward to avoid runaway cost) is the natural way to convert "a backup
that has never been restored" into "a backup restored last Tuesday,
verified, and torn down" as a standing operational fact rather than a
one-time drill. Not built this phase — consistent with README §23's
"CI/CD via GitHub Actions" also being out of scope, and this being the
same category of automation.

## 6. Phase 9 (backup/restore half) completion checklist

- [x] Backup/PITR configuration specified with `gcloud` commands (§1)
- [x] PITR behavior explained, including the "always a new instance"
      gotcha (§2)
- [x] Executable, STAGING-labeled restore exercise with real commands
      (§3)
- [x] Explicit refusal to fabricate a result, with reasoning (§4)
- [x] Path to future automation identified, correctly scoped out (§5)
- [ ] The exercise in §3 actually run and its template filled in —
      **not done this session**
