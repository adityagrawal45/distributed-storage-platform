# NimbusFS — Disaster Recovery Deployment Integration (Phase 12)

Companion to `docs/disaster-recovery.md` (Phase 9 — the actual DR
architecture, RTO/RPO targets, and the 9-step failover runbook this
file maps onto) and `docs/ci-cd.md`/`docs/deployment.md` (the
automation this file connects to it). This is NOT a rewrite of Phase
9's DR design — it exists solely to answer Phase 12 §33's specific
question: **can the deployment automation actually support that DR
plan, or does recovery silently depend on an undocumented manual
step?** Same DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline as every
other Phase 11/12 doc.

---

## 1. The question this document answers

`docs/disaster-recovery.md` §9's failover runbook was written as a
human-executed procedure, before this phase's CI/CD pipeline existed.
Re-reading it against what `.github/workflows/`, `scripts/ci-deploy.sh`,
and `terraform/` now provide:

| Runbook step (`disaster-recovery.md` §9) | Can Phase 12's pipeline do this? |
|---|---|
| 1. Declare the incident | **No — and should not be.** A human judgment call by design (§9 says so explicitly); automating "is this a real regional outage" risks an automated failover triggered by a false positive, which is strictly worse than a slower human-confirmed one. |
| 2. Restore the database in Region B | No — `docs/backup-restore.md`'s restore procedure is Cloud-SQL-native (`gcloud sql backups restore` / cross-region replica promotion), out of this pipeline's scope entirely. Nothing in `.github/workflows/` touches Cloud SQL. |
| 3. Verify Secret Manager entries in Region B | Partially — see §3 below on the Secret Manager gap this inherits from Phase 10/12. |
| **4. Scale up Region B's GKE Deployments from the region-agnostic manifests** | **Yes — this is exactly `scripts/ci-deploy.sh`.** The runbook step's own text ("already defined via the same manifests... no new YAML needed") is precisely what `ci-deploy.sh`'s `PROJECT_ID`/`REGION` substitution was built for — see §2 below. |
| **5. Point Region B's app at the restored DB/GCS via ConfigMap/Secret** | Partially — the `ConfigMap` substitution is covered by the same mechanism as step 4; the `Secret` (real credential values) is explicitly out of this pipeline's reach by design (`docs/ci-cd.md` §4 — no CI identity has Secret-read access) and stays a human/out-of-band step, matching Phase 9's own §7. |
| **6. Run startup verification (`/health`/`/ready`)** | **Yes — this is `scripts/k8s-smoke-test.sh`**, unchanged from Phase 5, already the exact mechanism `deploy-staging.yml`/`deploy-production.yml` run after every normal deploy. |
| 7. Add Region B to the load balancer / update DNS | No — Global load balancer backend/DNS changes are Terraform/`gcloud`-level infrastructure changes (`terraform/vpc.tf`-adjacent, not provisioned by this module at all — DNS is explicitly manual per `terraform/README.md`). |
| 8. Monitor error rates + reconciliation | Partially — Phase 11's dashboards (`docs/monitoring.md`) and Phase 9's reconciliation CronJob (already deployed by `ci-deploy.sh`'s full manifest set, per `docs/ci-cd.md` §9) both already run continuously in Region B once step 4 completes; nothing new needed here. |
| 9. Declare recovery, record timestamps | No — reporting/judgment, not automation. |

**Bottom line**: Phase 12's pipeline closes the APPLICATION-DEPLOYMENT
portion of the runbook (steps 4 and 6, partially 5 and 8) — the same
two steps that were previously "re-run some `kubectl`/`gcloud` commands
by hand, correctly, under incident pressure, in a region you may not
routinely deploy to." It does **not** and should not automate the
data-restore, DNS, or incident-declaration steps — those remain
deliberately manual, exactly as Phase 9 designed them, for reasons
that don't disappear just because CI now exists.

## 2. How, concretely: reusing the SAME pipeline for Region B

`scripts/ci-deploy.sh` (`docs/ci-cd.md` §9) already takes
`PROJECT_ID`/`REGION`/`IMAGE_TAG` as environment variables rather than
hardcoding them — the exact design choice that makes it region-
agnostic. Recovering into Region B means running the SAME
`deploy-production.yml` workflow (or `ci-deploy.sh` directly, run by a
human from a incident-response shell if GitHub Actions itself is
judged too slow to invoke under the specific incident) with:

```
PROJECT_ID = <Region B's project, or the same project if single-project>
REGION     = <Region B's region>
IMAGE_TAG  = <the last known-good git SHA already in Artifact Registry>
```

No new workflow file, no new script — this is precisely why
`docs/deployment.md` §1 recommends separate GCP projects per
environment (the same recommendation, read through a DR lens: the
"staging" project's deploy identity/cluster-name variables are already
a template for what a "Region B" set of GitHub Environment variables
would look like). **This has never been exercised against a real
second region or project** — see §5.

## 3. The Secret Manager gap, inherited

`docs/infrastructure.md` §7 already records "no Secret Manager
integration" as an open gap carried from Phase 10. In a DR context
specifically, this means Region B's Kubernetes Secret (DB password,
JWT key, Redis auth) must already exist there BEFORE step 4 runs, or
step 4's Pods will crash-loop on startup (Phase 4's `FAIL_FAST_ON_STARTUP`
means a missing/wrong secret is a loud, immediate failure — not a
silent one, which is the right failure mode, but it does mean this
prerequisite is real and unautomated). This is the single sharpest
edge in this whole DR-deployment story: **nothing in this pipeline
creates or replicates a secret into a second region**, and closing
that gap for real is the Secret Manager migration `docs/infrastructure.md`
§7 already describes, not a new piece of work this document invents.

## 4. What Phase 9's HA (not DR) mechanisms already give the pipeline for free

Within a SINGLE region, Phase 9's `topologySpreadConstraints` +
multi-zone node pool already mean `ci-deploy.sh`'s normal rolling
update is zone-redundant without any DR-specific code path — a zone
failure during a routine deploy is absorbed by the existing rollout
mechanics (`docs/deployment.md` §2), not something this document adds
anything to.

## 5. Honesty statement

**Nothing in this document has been executed.** No second GCP
project/region, no real Cloud SQL cross-region restore, and no real
GKE cluster in a second region have ever existed in any session that
worked on this repository. The mapping in §1's table is a reasoned
analysis of what the ALREADY-WRITTEN pipeline code would do if pointed
at a second region's variables — parameterization that is real and
inspectable in `scripts/ci-deploy.sh` — not a rehearsed or measured
recovery. `docs/disaster-recovery.md` §9 itself already carries the
same "DESIGNED, NOT TESTED end-to-end" label; this document does not
upgrade that label, it only clarifies which steps the Phase 12
pipeline would actually help with when someone does rehearse it.
