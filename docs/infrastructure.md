# NimbusFS — Infrastructure as Code (Phase 12)

Companion to `terraform/README.md` (Phase 9's own module documentation
— NOT superseded by this file; read that one for what each `.tf` file
contains) and `docs/ci-cd.md` §4 (CI authentication into this
infrastructure). Same DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline
— see `docs/observability.md`'s opening section.

---

## 1. What already existed (Phase 12 §3, inspection)

`terraform/` (Phase 9's extension) already manages VPC, GKE, the 6
runtime service accounts + Workload Identity bindings, the application
GCS bucket, the 3 Pub/Sub topics/subscriptions, and the Artifact
Registry repo. `terraform/README.md`'s own "State" section already
flagged local state as something to fix "before a second person or a
CI pipeline ever runs this" — this phase is that fix, not a
rediscovery of the problem.

**Not managed by Terraform, and deliberately still manual** (unchanged
by this phase, per Phase 9's own explicit scoping): Cloud SQL,
Memorystore, DNS, Secret values, and `k8s/*.yaml` itself (`kubectl
apply` stays the deploy path for the application layer — see
`docs/ci-cd.md`'s "why not Argo CD" for the same reasoning applied to
the deploy mechanism, not just Terraform's scope).

## 2. What Phase 12 adds

`terraform/cicd.tf` — the WIF pool/provider and 4 CI-only service
accounts (`nimbusfs-ci-build`, `nimbusfs-ci-deploy`,
`nimbusfs-ci-deploy-prod`, `nimbusfs-ci-terraform-plan`,
`nimbusfs-ci-terraform-apply`) — see `docs/ci-cd.md` §4 for the full
identity table and trust conditions. Nothing here touches or
re-provisions any Phase 9 resource; it is purely additive.

## 3. Infrastructure ownership matrix (Phase 12 §4)

| Resource | Managed by | Environment | State location | Who can modify |
|---|---|---|---|---|
| VPC, subnet, Cloud NAT | Terraform (`vpc.tf`) | per-project | GCS remote state (see §4) | `nimbusfs-ci-terraform-apply` (CI, approval-gated) or a human with `terraform apply` + project credentials |
| GKE cluster + node pool | Terraform (`gke.tf`) | per-project | GCS remote state | same as above |
| Runtime GSAs (app + 5 workers) + Workload Identity bindings | Terraform (`iam.tf`) | per-project | GCS remote state | same as above |
| CI GSAs + WIF pool/provider | Terraform (`cicd.tf`, this phase) | per-project | GCS remote state | same as above — note this means the identity that can modify CI's OWN credentials is the terraform-apply identity, not any CI-deploy identity; see §5 |
| GCS bucket (application files) | Terraform (`storage.tf`) | per-project | GCS remote state | same as above |
| Pub/Sub topics/subscriptions | Terraform (`pubsub.tf`) | per-project | GCS remote state | same as above |
| Artifact Registry repo | Terraform (`artifact_registry.tf`) | per-project | GCS remote state | same as above |
| Cloud Monitoring alert policies + uptime check | Terraform (`monitoring.tf`, Phase 11) | per-project | GCS remote state | same as above |
| Kubernetes Deployments/Services/etc. (`k8s/*.yaml`) | `kubectl apply`, driven by CI (`scripts/ci-deploy.sh`) or a human (`scripts/k8s-deploy.sh`) | per-cluster | the live cluster's own etcd (no separate "state file" — Kubernetes IS its own state store) | `nimbusfs-ci-deploy` / `nimbusfs-ci-deploy-prod` (CI), or a human with `kubectl` access |
| Container image | Built by CI (`ci.yml`), stored in Artifact Registry | — | the registry itself | `nimbusfs-ci-build` (push only — cannot delete/overwrite an existing immutable tag) |
| Secret **values** (JWT signing key, DB/Redis passwords) | Created out-of-band by a human (`kubectl create secret`, per `k8s/README.md`) | per-cluster | the Kubernetes Secret object (or, ideally, GCP Secret Manager — see §6) | a human with `kubectl` access; **no CI identity has any IAM role granting Secret Manager or Kubernetes Secret read/write** |
| Cloud SQL, Memorystore, DNS | Manual (`gcloud`/Console), per `k8s/README.md` | per-project | GCP's own resource state | a human |

**No resource is managed through two conflicting systems.** The one
resource that could look like an exception — `k8s/*.yaml`'s
`<PROJECT_ID>`/`REGION` placeholders being filled in by BOTH a human
(`k8s/README.md`'s manual step, for `scripts/k8s-deploy.sh`) and CI
(`scripts/ci-deploy.sh`'s `sed` substitution) — is not: both consume
the SAME source files and produce the SAME substitution, one path for
humans, one for CI; neither writes back a "resolved" copy of the
manifests to disk that the other could then diverge from.

## 4. Terraform state (Phase 12 §18)

**Still local by default as of this phase** (`terraform/versions.tf`'s
commented-out `backend "gcs"` block, unchanged) — genuinely fixing
this requires a real GCS bucket to point the backend at, which has the
classic bootstrapping problem: Terraform can't manage the state bucket
its own state lives in (chicken/egg). The correct, standard resolution
— documented here as the next concrete step, NOT executed this
session (no real GCP project exists) —:

```
1. Create the state bucket OUTSIDE this module, e.g. via a one-time
   `gcloud storage buckets create gs://nimbusfs-terraform-state
   --location=us-central1 --uniform-bucket-level-access` +
   `gcloud storage buckets update ... --versioning` (or a tiny,
   separate one-resource Terraform config with ITS OWN local state,
   applied once, that never touches anything else).
2. Restrict access to that bucket to exactly: the terraform-apply CI
   identity (read+write) and the terraform-plan CI identity (read-only
   — `roles/storage.objectViewer`, so a PR's plan step can read
   current state to compute an accurate diff, but can never corrupt
   or delete it).
3. Uncomment terraform/versions.tf's backend "gcs" block, fill in the
   real bucket name, run `terraform init -migrate-state` ONCE from
   wherever state currently lives locally.
4. Delete the local .tfstate file only after confirming the migrated
   remote state is intact (`terraform state list` against the new
   backend).
```

GCS-backend locking (Terraform >= 1.x's native GCS backend uses
object generation preconditions for locking, no separate DynamoDB-
style lock table needed) is what prevents two concurrent `terraform
apply` runs from corrupting state — this is exactly why `terraform.yml`'s
`apply` job sets `concurrency: group: terraform-apply` (belt-and-
suspenders: GitHub-side serialization AND GCS-side locking, not
relying on either alone).

**State is never committed to git** — `terraform/*.tfstate*` has been
gitignored since Phase 9 (`.gitignore`), unchanged.

## 5. Terraform plan/apply gate (Phase 12 §19)

Already covered in full in `docs/ci-cd.md` §4's identity table and
`terraform.yml`'s own inline comments — summarized: every PR touching
`terraform/**` gets a read-only `plan` (posted as a PR comment, never
applies), and `apply` runs ONLY via manual `workflow_dispatch`
requiring the `production-terraform` GitHub Environment's approval,
enforced independently at BOTH the GitHub-environment layer and the
GCP WIF-trust-condition layer (`terraform/cicd.tf`'s
`ci_terraform_apply_wif` — a token is only trusted if it already
carries `environment=production-terraform`, which GitHub only stamps
onto a token after that environment's protection rules already
passed).

## 6. Terraform security review (Phase 12 §17)

Findings from re-reading every `.tf` file with this phase's specific
checklist in mind:

| Check | Finding |
|---|---|
| Hardcoded secrets | None. Every sensitive value (`master_authorized_networks`, `project_id`) is a variable, sourced from `terraform.tfvars` (gitignored) or CI repo variables — never a literal in a `.tf` file. |
| Overly broad IAM | The 6 runtime GSAs (Phase 9) are already least-privilege per-worker (see `iam.tf`'s own header comment). This phase's 5 CI GSAs are scoped per-job (`docs/ci-cd.md` §4); the widest, `nimbusfs-ci-terraform-apply`, is enumerated per-service (no `roles/owner`/`roles/editor`) and gated behind a required-reviewer environment — see §5. |
| Public buckets | `storage.tf`'s bucket has no `google_storage_bucket_iam_member` granting `allUsers`/`allAuthenticatedUsers` anywhere in this module — access is exclusively per-GSA bindings. |
| Public databases/Redis | Not provisioned by this module at all (Cloud SQL/Memorystore are manual, per §1) — nothing to review here YET; when they ARE terraformed, `docs/high-availability.md`'s existing "private IP only" guidance already sets the expectation. |
| Insecure firewall rules | `vpc.tf`'s GKE cluster is `private` (no public node IPs); `k8s/11-networkpolicy.yaml`'s default-deny-all is the actual traffic boundary (Kubernetes-native, not a GCP firewall rule) — reviewed again in this phase for the Phase 11 `gmp-system` addition, unchanged conclusion: no rule here is broader than it needs to be. |
| Unencrypted state | GCS's default server-side encryption applies to ANY bucket, including a future state bucket — no additional Terraform configuration is required for that baseline; customer-managed encryption keys (CMEK) are not configured (would be a deliberate additional step, not needed at this project's current threat model). |
| State stored locally | **Still true today** — see §4's concrete remediation, not yet executed. |
| Missing locking | GCS backend locking is automatic once §4's remote backend is actually wired up — not yet, since state is still local. |
| Dangerous `terraform destroy` | No workflow in `.github/workflows/` runs `terraform destroy` under any trigger — it is not automatable by this pipeline at all; a destroy remains a manual, deliberate `terraform destroy` invocation by a human with direct credentials. |
| Accidental production changes | `terraform.yml`'s `plan` job (PRs) NEVER applies (§5); `apply` requires the `production-terraform` environment's manual approval on every single invocation — there is no "auto-apply on merge" path anywhere in this pipeline. |

## 7. Secret Manager (a real, open gap — Phase 10 already recorded this)

Secret values (`JWT_SECRET_KEY`, DB/Redis passwords) live in a
Kubernetes Secret today, created out-of-band by a human — Phase 10's
`docs/security/final-report.md` already recorded "no Secret Manager
integration" as a remaining risk, and this phase does not close it.
Migrating to GCP Secret Manager (with the running application's
Workload-Identity-bound GSA granted `roles/secretmanager.secretAccessor`
scoped to specific secret resources, and Kubernetes reading them via
the Secret Manager CSI driver rather than a plain Kubernetes Secret)
is the natural next step — genuinely out of scope for a CI/CD phase to
retrofit as a side effect, recorded here so it isn't lost between
phase writeups.

## 8. Honesty statement

**No Terraform resource in this repository — Phase 9's, Phase 11's,
or this phase's — has been applied against a real GCP project.**
`terraform validate`/`plan` were run against Phase 9's original module
during that phase's own session (a temporarily-downloaded `terraform`
binary, removed after use — see `terraform/README.md`). No
`terraform` binary was installed in THIS session; `cicd.tf` is
reviewed by eye against the `google` provider's documented resource
schemas only, matching Phase 11's `monitoring.tf`'s already-recorded
lower-confidence caveat.
