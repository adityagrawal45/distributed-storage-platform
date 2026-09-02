# NimbusFS — Terraform (GKE + VPC + IAM)

## What this is

Terraform for the **GCP infrastructure layer** that `k8s/README.md`'s
"One-time cluster setup" and "Workload Identity setup" sections
currently document as a manual `gcloud`/`gsutil` runbook: a dedicated
VPC, a private regional GKE cluster with Workload Identity, an
Artifact Registry repo, a static IP for the Ingress, and — the actual
point of this module — 6 IAM service accounts (one per k8s Deployment
that needs GCP credentials) with least-privilege roles scoped to real
resources, bound to the matching Kubernetes ServiceAccount via
Workload Identity.

This module **replaces the manual steps with `terraform apply`**; it
does not replace or duplicate anything in `k8s/` (those manifests are
still applied with `kubectl` as documented in `k8s/README.md`) or
change any application code.

## What this deliberately does NOT do (scope, as of Phase 9)

- **No Cloud SQL, no Memorystore.** `docs/high-availability.md`
  documents Regional-HA Cloud SQL and Standard-tier Memorystore as
  *design targets*, never applied. This module doesn't apply them
  either — that's a separate pass. Follow `k8s/README.md`'s
  "Cloud SQL & Memorystore" section manually until it exists.
- **No CI/CD.** Explicitly future-phase per `CONTEXT.md`'s "Not yet
  built" list.
- **No `terraform apply` has been run against a real project by this
  module.** It was written and reviewed, not executed — there is no
  GCP project set up for NimbusFS (`gcloud config`'s active project at
  time of writing was an unrelated project). Run `terraform plan`
  yourself against a real `project_id` before `apply`, the same
  "verify before trusting" posture `alembic/versions/0005_*`'s own
  docstring takes toward its own untested migration.
- **Does not template `k8s/*.yaml` into Terraform.** `kubectl apply`
  stays the deploy path for the application layer; this module stops
  at provisioning the GCP resources those manifests assume already
  exist (the cluster, the 6 GSAs, the bucket, the topics).

## Why the GCS bucket and Pub/Sub topics/subscriptions are here too

This module was scoped as "GKE + VPC + IAM," but IAM roles need a real
resource to be scoped *to* — granting `roles/storage.objectAdmin` or
`roles/pubsub.publisher` at the **project** level instead would be the
exact blast-radius mistake `k8s/16-worker-serviceaccounts.yaml`'s
per-worker-GSA design argues against. So `storage.tf` and `pubsub.tf`
create the one bucket and three topics/subscriptions IAM binds to —
gated behind `create_gcs_bucket` / `create_pubsub_topics` (both default
`true`) so a future Terraform pass that manages those differently
(e.g. alongside Cloud SQL/Memorystore) can turn this module's copies
off without conflict.

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: real project_id, master_authorized_networks

terraform init
terraform plan   # READ the plan — this creates real, billed resources
terraform apply
```

After `apply` succeeds, follow the `next_steps` output — in short:
fill in the 6 `iam.gke.io/gcp-service-account` annotations in
`k8s/03-serviceaccount.yaml` and `k8s/16-worker-serviceaccounts.yaml`
with the `service_account_emails` output, then continue with
`k8s/README.md` from "Cloud SQL & Memorystore" onward (unchanged by
this module).

## State

Local state by default (`versions.tf`). Before a second person or a
CI pipeline ever runs this, switch to a `gcs` backend pointed at a
**dedicated** state bucket — not `nimbusfs-files-*`, which is
application data with a completely different access-control story.
The commented-out block in `versions.tf` is where that goes.

## File map

| File | Contents |
|---|---|
| `versions.tf` | Provider pinning, backend placeholder |
| `variables.tf` | All inputs — every default documented inline against what it must match in `k8s/` |
| `vpc.tf` | Dedicated VPC, VPC-native subnet w/ secondary ranges, Cloud NAT (a real gap `k8s/README.md`'s manual steps had — private nodes need it and the runbook never provisioned it) |
| `gke.tf` | Private regional cluster + `nimbusfs-app-pool` node pool, matching `07-deployment.yaml`'s `nodeAffinity` |
| `storage.tf` | The application GCS bucket, so IAM has something to scope to |
| `pubsub.tf` | The 3 Phase 8 topics + 3 subscriptions, names matching `app/core/config/settings.py` exactly |
| `iam.tf` | **The core deliverable** — 6 GSAs, roles scoped per `k8s/16-worker-serviceaccounts.yaml`'s table, 6 Workload Identity bindings |
| `artifact_registry.tf` | Image repo + Ingress static IP |
| `outputs.tf` | Everything you need to complete the manual steps this module doesn't cover |
