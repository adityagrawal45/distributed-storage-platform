# ---------------------------------------------------------------------
# Provider/version pinning
# ---------------------------------------------------------------------
# This Terraform module codifies exactly the manual `gcloud`/`gsutil`
# runbook already documented in k8s/README.md ("One-time cluster setup"
# through "Workload Identity setup") — it does not introduce a new
# architecture, it makes the existing documented one reproducible and
# reviewable as a diff instead of a checklist someone can run out of
# order or half-apply. See terraform/README.md for what this replaces
# and what it deliberately still leaves manual (Cloud SQL, Memorystore,
# Secrets, DNS, image build/push — unchanged from k8s/README.md).
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }

  # Local state by default — matches this being a from-scratch module
  # nothing else in the repo has wired a remote backend for yet. Before
  # a second person or a CI pipeline ever runs this, replace this block
  # with a `gcs` backend pointed at a dedicated state bucket (NOT the
  # nimbusfs-files-* application bucket — state needs its own
  # versioned, restricted-access bucket). See terraform/README.md.
  #
  # backend "gcs" {
  #   bucket = "<your-terraform-state-bucket>"
  #   prefix = "nimbusfs/terraform/state"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
