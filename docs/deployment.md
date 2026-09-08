# NimbusFS — Deployment (Phase 12)

Companion to `docs/ci-cd.md` (pipeline that drives this) and
`docs/infrastructure.md` (the Terraform that provisions what's
deployed into). Same DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline —
see `docs/observability.md`'s opening section.

---

## 1. Environment strategy (Phase 12 §5)

Three environments, names matching `terraform/variables.tf`'s
pre-existing `environment` variable exactly (per the brief's explicit
"preserve existing naming" instruction):

| Environment | Where it runs | Purpose |
|---|---|---|
| **development** | `docker-compose.yml`, a developer's own machine | Local iteration — no GKE, no CI involvement at all. |
| **staging** | GKE, deployed automatically on every merge to `main` | Validates migrations, deployment, worker rollout, and health checks against real infrastructure before anyone considers production (Phase 12 §36). |
| **production** | GKE, deployed only via an approved manual `workflow_dispatch` | Real user traffic. |

### Recommended isolation: separate GCP projects

`terraform/variables.tf`'s `environment` variable already lets ONE
Terraform module produce a differently-named GCS bucket per
environment (`nimbusfs-files-dev`/`-staging`/`-prod`), which is enough
for a single-project setup. **This document recommends going
further**: apply the module once per environment into **separate GCP
projects** (e.g. `nimbusfs-staging`, `nimbusfs-prod`), each with its
own GKE cluster, Cloud SQL instance, and Memorystore instance.

Why this matters more than it might look: a single shared project
means staging and production share IAM policy, VPC, and — critically
— the blast radius of any misconfiguration. `terraform/cicd.tf`'s
`nimbusfs-ci-deploy` (staging) and `nimbusfs-ci-deploy-prod`
(production) identities are ALREADY separate specifically so that a
compromised staging credential cannot touch production — but if both
environments' clusters sit in the same project, a `roles/container.developer`
grant scoped at the project level (the residual-scope gap
`terraform/cicd.tf` already documents) means `nimbusfs-ci-deploy`
technically has API access to the production cluster too, `container.developer`
role or not. Separate projects close that gap structurally — there is
no cluster to reach.

**This is a recommendation, not something applied this session** — no
second GCP project was created, and doing so is a real infrastructure
decision with real cost implications for the user to make deliberately
via `terraform.tfvars`/CI variables (`GCP_PROJECT_ID_STAGING` /
`GCP_PROJECT_ID_PRODUCTION`, already referenced by name in
`.github/workflows/deploy-*.yml`), not something to default into
silently.

### What must never happen (Phase 12 §5's explicit warning)

Production credentials/data must never flow into development. Nothing
in this pipeline makes that possible even by accident:
`deploy-staging.yml`/`deploy-production.yml` each authenticate with
their OWN CI identity against their OWN target project/cluster (no
shared kubeconfig, no shared credential); `.env`/`k8s/06-secret.yaml`
are gitignored and were never templated with real values in any
committed file; and `docker-compose.yml` (development) has no GCP
credentials wired into it at all — see its own file for the local-only
Postgres/Redis containers it runs instead.

## 2. Deployment strategy (Phase 12 §21-22)

**Rolling update**, unchanged from Phase 5's already-correct design —
this phase's brief explicitly says to prefer the existing approach
when it works, and it does:

- `maxUnavailable: 0` / `maxSurge: 1` (`k8s/07-deployment.yaml`) — a
  new Pod must pass readiness before an old one is removed; true
  zero-downtime, safe specifically because the app is stateless
  (Phase 4).
- Readiness probe hits `/api/v1/ready` (DB+Redis+Storage checked);
  liveness probe hits `/api/v1/live` (no dependency checks) — see
  `docs/observability.md` §8 for why conflating the two would let a
  Cloud SQL blip trigger a fleet-wide restart storm.
- `PodDisruptionBudget` (`k8s/10-pdb.yaml`) + `topologySpreadConstraints`
  (Phase 9) ensure a voluntary disruption (node drain, cluster upgrade)
  never drops below a safe replica count across zones.
- Graceful shutdown: `docker/Dockerfile`'s exec-form `CMD` lets SIGTERM
  reach `uvicorn` directly, which runs `app/main.py`'s lifespan
  shutdown hook (stop accepting new work, close DB/Redis pools) —
  unchanged since Phase 4, verified still correct, not re-implemented.

```
New image tag pushed (CI)
    |
    v
kubectl set image / apply (scripts/ci-deploy.sh)
    |
    v
New ReplicaSet creates 1 Pod (maxSurge: 1) -> now 4/3 desired
    |
    v
New Pod: startup -> readiness probe passes -> receives traffic
    |
    v
1 old Pod terminates (preStop grace + SIGTERM) -> back to 3/3
    |
    v
Repeat until all 3 replicas are the new version
```

## 3. Canary / progressive delivery (Phase 12 §26)

**Not implemented. Rolling updates are sufficient at NimbusFS's
current scale**, and the brief explicitly asks to say so rather than
implement canary/blue-green/progressive delivery by default. Reasoning:

- A canary/blue-green setup earns its cost when a bad release needs to
  be caught against a SLICE of real production traffic before it's
  fully rolled out — valuable at high request volume, where the
  smoke-test-and-auto-rollback flow this phase already has
  (`docs/ci-cd.md` §8) might not surface a subtle regression fast
  enough. NimbusFS has no measured production traffic at all yet (see
  `docs/slo.md`'s honesty statement, Phase 11) — there is no traffic
  pattern to canary against.
- The existing rollout strategy already provides the two properties
  that matter most pre-scale: a bad Pod never receives traffic
  (readiness-gated) and a bad rollout is caught fast and reversed
  automatically (`docs/ci-cd.md`'s smoke-test-then-rollback step).
- **Revisit when**: real measured traffic exists AND a past incident
  was specifically caused by a regression the rolling-update +
  smoke-test flow didn't catch before 100% of traffic saw it — that is
  the concrete trigger condition, not "canary is a best practice."

## 4. Database migrations (Phase 12 §23)

`alembic/versions/` already exists (Phases 1-2-3-6-8) and has never
been run against a real Postgres in ANY prior phase (recorded
honestly in every prior migration's own docstring). This phase does
not change that, and does not add automatic `alembic upgrade head`
execution to any deploy workflow — running a schema migration
automatically, unattended, as part of every rollout is exactly the
kind of irreversible, high-blast-radius action Phase 12 §23 warns
against doing blindly.

**The expand/contract discipline this project needs going forward**,
documented here since no migration has tested it yet:

```
Expand   : add the new column/table/index as NULLABLE or with a
           default — the OLD application version must keep working
           against this schema unmodified (it simply never reads/
           writes the new column).
Deploy   : roll out the new application version (rolling update means
           OLD and NEW code run simultaneously for the rollout's
           duration — this is exactly why the expand step must be
           backward-compatible with the code being replaced).
Backfill : populate the new column for existing rows, in batches, as
           its own step — never inside the same transaction as the
           schema change on a large table (locks it for the duration).
Switch   : once 100% of Pods are running NEW code and backfill is
           complete, the new code path is the only one exercised.
Contract : a LATER migration drops the old column/constraint the new
           code no longer needs — only after confirming no rollback
           to the old application version is still possible (see §5).
```

**Concretely for NimbusFS's rolling-update strategy**: because
`maxUnavailable: 0`/`maxSurge: 1` guarantees old and new Pods run
side-by-side for the rollout's duration, a migration that (for
example) renames a column instead of adding a new one would break
every request the OLD Pods serve until they're all replaced — this is
the exact "avoid migrations that make old and new incompatible during
a rolling deployment" failure Phase 12 §23 calls out, and expand/
contract is what avoids it.

This phase adds no CI automation for `alembic upgrade head` — running
it remains a deliberate, manual, documented step (see `k8s/README.md`'s
existing migration guidance), consistent with treating an unreviewed
automatic production schema change as unacceptable regardless of how
good CI's other gates are.

## 5. Deployment verification & rollback

See `docs/ci-cd.md` §8 (verification) and `docs/rollback.md` (the full
rollback strategy, including why database rollback is deliberately
NOT "run the down-migration").

## 6. Honesty statement

**Nothing in this document was executed against a real GKE cluster,
this session or any prior one.** No real GCP project or reachable
cluster has ever been available in any session that touched this
repository (`CONTEXT.md`'s "Phase 5 Verification Caveat" records the
same gap for the original manifests: `docker ps` failed, Docker
Desktop wasn't running, so even `kind`/`minikube` weren't an option).
The rolling-update mechanics described above (readiness-gated traffic,
self-healing, `kubectl rollout undo`) are DESIGNED and match
Kubernetes' own documented Deployment controller behavior exactly —
they are NOT independently MEASURED against a real cluster by this
project at any point. `scripts/k8s-smoke-test.sh --full` exists
specifically to produce that measurement (self-healing + rollout/
rollback demos) the day a real cluster is reachable — running it, for
the first time, is the correct next verification step before trusting
any claim in this document beyond "the YAML is syntactically valid and
matches Kubernetes' documented schema."
