# ---------------------------------------------------------------------
# CI/CD identities — Workload Identity Federation for GitHub Actions
# (Phase 12)
# ---------------------------------------------------------------------
# This file provisions the GCP-side identity plumbing for
# `.github/workflows/*.yml` to authenticate to GCP with ZERO long-lived
# credentials — no service-account JSON key is ever generated, stored
# in a GitHub Secret, or downloaded by anyone. See docs/ci-cd.md
# "Authentication" for the full request/response flow this sets up:
#
#   GitHub Actions job
#     -> requests a short-lived OIDC ID token from GitHub's own OIDC
#        provider (token.actions.githubusercontent.com), scoped to
#        THIS repo/workflow/ref/environment
#     -> exchanges it, via the WIF pool+provider below, for a
#        short-lived (1h) GCP access token impersonating one of the
#        4 CI service accounts below
#     -> that access token is all `gcloud`/`kubectl`/`terraform` in the
#        job ever see. It expires on its own; there is nothing to leak
#        that outlives the job run.
#
# Four separate CI identities (Phase 12 brief §16's explicit
# requirement: "Create separate identities for CI Build / CI Deploy /
# Terraform / Runtime — do not give CI Owner or unrestricted project
# permissions"). "Runtime" identities are the 6 GSAs `iam.tf` already
# created in Phase 9 for the running application/workers — unchanged,
# unrelated to CI, and never impersonable by any CI identity below.
#
# NOT applied against a real GCP project this session — see
# docs/ci-cd.md's DESIGNED/IMPLEMENTED/TESTED/MEASURED table. Every
# `google_iam_workload_identity_pool*` / `google_service_account*`
# resource shape here matches the `google` provider's documented
# schema (reviewed by eye, `terraform validate` not re-run — no
# `terraform` binary was installed this session, same caveat
# `terraform/monitoring.tf` already carries from Phase 11).

resource "google_iam_workload_identity_pool" "github_actions" {
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions"
  description               = "Federates GitHub Actions OIDC tokens for ${var.github_repository} — no GCP service-account keys are issued to CI."
}

resource "google_iam_workload_identity_pool_provider" "github_actions" {
  workload_identity_pool_id         = google_iam_workload_identity_pool.github_actions.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions-provider"
  display_name                      = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    # GitHub's OIDC token only carries an `environment` claim when the
    # calling job declares `environment: <name>` AND that environment's
    # protection rules have already been satisfied (e.g. a required
    # reviewer approved it) — this is what makes the attribute_condition
    # on the apply-identity binding below a REAL gate, not merely a
    # workflow-YAML convention a compromised workflow file could bypass.
    "attribute.environment" = "assertion.environment"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  # Hard boundary at the POOL PROVIDER level, independent of anything
  # any individual service-account binding does below: a token asserting
  # a `repository` other than this exact repo is rejected before any
  # impersonation is even considered. This is what stops a compromised
  # or malicious workflow in a DIFFERENT GitHub repo from ever being
  # able to mint a GCP token against this project, even if it somehow
  # knew a GSA email here.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""
}

# ---------------------------------------------------------------------
# CI Build — Artifact Registry push only. Runs on every PR/push-to-main
# build (.github/workflows/ci.yml).
# ---------------------------------------------------------------------
resource "google_service_account" "ci_build" {
  account_id   = "nimbusfs-ci-build"
  display_name = "CI: build & push container image (GitHub Actions, WIF)"
}

resource "google_artifact_registry_repository_iam_member" "ci_build_writer" {
  location   = google_artifact_registry_repository.nimbusfs.location
  repository = google_artifact_registry_repository.nimbusfs.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci_build.email}"
}

resource "google_service_account_iam_member" "ci_build_wif" {
  service_account_id = google_service_account.ci_build.name
  role                = "roles/iam.workloadIdentityUser"
  # Any workflow run in THIS repo (any branch/PR) may build and push an
  # image — pushing is not itself a deployment, and Artifact Registry
  # tags are immutable-by-convention (git SHA — see docs/ci-cd.md
  # "Immutable tags"), so a PR building an image costs nothing and
  # risks nothing beyond registry storage.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_actions.name}/attribute.repository/${var.github_repository}"
}

# ---------------------------------------------------------------------
# CI Deploy — rolls a built image out to GKE (staging automatically on
# main, production only via an approved workflow_dispatch).
# ---------------------------------------------------------------------
resource "google_service_account" "ci_deploy" {
  account_id   = "nimbusfs-ci-deploy"
  display_name = "CI: deploy to GKE (GitHub Actions, WIF)"
}

# roles/container.developer grants get/list/update on workloads
# (Deployments, Pods, Services, etc.) within the cluster — enough for
# `kubectl apply`/`kubectl set image`/`kubectl rollout status`/
# `kubectl rollout undo`, but NOT `roles/container.admin` (which also
# grants cluster-config-mutating operations like node pool changes,
# never something a deploy step should be able to do). Granted at
# PROJECT level because Google Cloud IAM does not support conditioning
# `container.*` permissions on a specific cluster resource the way
# `google_storage_bucket_iam_member`'s `condition{}` block scopes GCS
# above (see iam.tf) — recorded here as a real, known residual-scope
# gap rather than implied away. With exactly one cluster in this
# project today, the practical blast radius is unchanged either way;
# it stops being true the day a second cluster is added, which is
# exactly when this should be revisited.
resource "google_project_iam_member" "ci_deploy_container_developer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.ci_deploy.email}"
}

resource "google_service_account_iam_member" "ci_deploy_wif" {
  service_account_id = google_service_account.ci_deploy.name
  role                = "roles/iam.workloadIdentityUser"
  # Gated to the "staging" environment claim specifically — the same
  # GitHub-stamps-this-claim-only-after-protection-rules-pass mechanism
  # documented on ci_terraform_apply_wif below, applied here to staging
  # auto-deploys (`.github/workflows/deploy-staging.yml` declares
  # `environment: staging`). Production is a SEPARATE identity
  # (ci_deploy_production below) with its own, separately-approved
  # environment — deliberately two identities, not one identity used
  # from two workflows, so a staging deploy can never be the thing that
  # accidentally has production credentials in scope.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_actions.name}/attribute.environment/staging"
}

# ---------------------------------------------------------------------
# CI Deploy — PRODUCTION. Same permission shape as ci_deploy, but a
# separate identity+binding so production access is never a side
# effect of whatever staging's environment happens to allow, and so
# the two are independently auditable in Cloud Logging admin activity
# logs (distinct service-account emails in every log entry).
# ---------------------------------------------------------------------
resource "google_service_account" "ci_deploy_production" {
  account_id   = "nimbusfs-ci-deploy-prod"
  display_name = "CI: deploy to GKE — PRODUCTION (GitHub Actions, WIF, approval-gated)"
}

resource "google_project_iam_member" "ci_deploy_production_container_developer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.ci_deploy_production.email}"
}

resource "google_service_account_iam_member" "ci_deploy_production_wif" {
  service_account_id = google_service_account.ci_deploy_production.name
  role                = "roles/iam.workloadIdentityUser"
  # THE production approval gate, enforced at the IDENTITY level: only
  # a workflow run whose OIDC token carries `environment=production`
  # can impersonate this account, and that claim only appears once
  # GitHub's own environment protection rules (required reviewers —
  # configured in repo Settings -> Environments, not representable in
  # this Terraform module) have already approved the run. See
  # docs/ci-cd.md "Production approval" for the full chain.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_actions.name}/attribute.environment/production"
}

# ---------------------------------------------------------------------
# Terraform (read-only) — runs `terraform plan` on every PR touching
# terraform/**, comments the plan, never applies.
# ---------------------------------------------------------------------
resource "google_service_account" "ci_terraform_plan" {
  account_id   = "nimbusfs-ci-terraform-plan"
  display_name = "CI: terraform plan (read-only, GitHub Actions, WIF)"
}

resource "google_project_iam_member" "ci_terraform_plan_viewer" {
  project = var.project_id
  role    = "roles/viewer"
  member  = "serviceAccount:${google_service_account.ci_terraform_plan.email}"
}

# Plan also needs to READ (never write) IAM policy bindings to compute
# an accurate diff for iam.tf's resources — roles/viewer alone does not
# include `iam.serviceAccounts.getIamPolicy`-class permissions.
resource "google_project_iam_member" "ci_terraform_plan_iam_viewer" {
  project = var.project_id
  role    = "roles/iam.securityReviewer"
  member  = "serviceAccount:${google_service_account.ci_terraform_plan.email}"
}

resource "google_service_account_iam_member" "ci_terraform_plan_wif" {
  service_account_id = google_service_account.ci_terraform_plan.name
  role                = "roles/iam.workloadIdentityUser"
  member              = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_actions.name}/attribute.repository/${var.github_repository}"
}

# ---------------------------------------------------------------------
# Terraform (apply) — the single most-privileged identity in this
# entire system. Provisions/modifies every resource this Terraform
# module manages (VPC, GKE, IAM, GCS, Pub/Sub, Artifact Registry,
# monitoring). Deliberately NOT project Owner/Editor — scoped to
# exactly the service-specific admin roles this module's own resource
# blocks require, enumerated rather than granted as a wildcard.
# ---------------------------------------------------------------------
resource "google_service_account" "ci_terraform_apply" {
  account_id   = "nimbusfs-ci-terraform-apply"
  display_name = "CI: terraform apply (GitHub Actions, WIF, approval-gated)"
}

resource "google_project_iam_member" "ci_terraform_apply_roles" {
  for_each = toset([
    "roles/compute.networkAdmin",       # vpc.tf
    "roles/container.admin",            # gke.tf
    "roles/storage.admin",              # storage.tf
    "roles/pubsub.admin",               # pubsub.tf
    "roles/artifactregistry.admin",     # artifact_registry.tf
    "roles/iam.serviceAccountAdmin",    # iam.tf (create/delete the 6 runtime GSAs)
    "roles/resourcemanager.projectIamAdmin", # iam.tf (grant roles TO those GSAs)
    "roles/monitoring.editor",          # monitoring.tf (Phase 11)
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ci_terraform_apply.email}"
}

resource "google_service_account_iam_member" "ci_terraform_apply_wif" {
  service_account_id = google_service_account.ci_terraform_apply.name
  role                = "roles/iam.workloadIdentityUser"
  # THE actual gate: only a workflow run whose OIDC token carries
  # `environment=production-terraform` may impersonate this identity —
  # and GitHub only stamps that claim onto the token after that
  # environment's own protection rules (required reviewers, configured
  # in GitHub repo settings, not in this file) have already been
  # satisfied. A workflow file cannot forge this by simply writing
  # `environment: production-terraform` in its YAML on a branch that
  # hasn't been reviewed — the claim only appears once GitHub itself
  # has gated the run.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_actions.name}/attribute.environment/production-terraform"
}
