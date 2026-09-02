# ---------------------------------------------------------------------
# GCS bucket (application file storage) — created only so IAM has a
# real resource to scope roles.storage.objectAdmin to, per
# variables.tf's create_gcs_bucket comment.
# ---------------------------------------------------------------------
# Naming and locality follow app/core/config/settings.py's
# GCS_BUCKET_NAME convention (nimbusfs-files-<env>) and
# docs/high-availability.md's regional-bucket-over-dual-region decision
# exactly — this does not introduce a new storage design, it provisions
# the one already chosen.

locals {
  gcs_bucket_name = var.gcs_bucket_name != "" ? var.gcs_bucket_name : "nimbusfs-files-${var.environment}"
}

resource "google_storage_bucket" "files" {
  count = var.create_gcs_bucket ? 1 : 0

  name     = local.gcs_bucket_name
  location = var.gcs_bucket_location
  project  = var.project_id

  # Private bucket, signed URLs only — matches app/database/gcs.py and
  # main README.md's Phase 3 design (uniform_bucket_level_access is the
  # modern replacement for legacy per-object ACLs; storage_service.py
  # never sets a per-object ACL, so nothing here depends on that path).
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true # cheap undo for accidental overwrite/delete; distinct from FileVersion's application-level versioning
  }

  # Soft-delete retention is a GCS-native safety net underneath the
  # app's own soft-delete (FileMetadata.status) and Phase 9's
  # reconciliation job — none of those protect against a bug that
  # issues a real DELETE against the bucket.
  soft_delete_policy {
    retention_duration_seconds = 7 * 24 * 60 * 60 # 7 days
  }

  # No storage-class lifecycle rule: not something any existing design
  # doc (docs/high-availability.md included) has decided on. Add one
  # deliberately in a future pass rather than defaulting it here.

  labels = var.labels
}
