# ---------------------------------------------------------------------
# IAM — 6 GSAs + Workload Identity bindings
# ---------------------------------------------------------------------
# This is the Terraform equivalent of k8s/README.md's "Workload
# Identity setup" `gcloud` block, extended to also cover the 5 Phase 8/9
# worker GSAs that block already documents by table
# (k8s/16-worker-serviceaccounts.yaml's header) but never scripted.
#
# Every GSA here is intentionally separate — not a shared "nimbusfs"
# account — for the exact reason 16-worker-serviceaccounts.yaml gives:
# a compromised notification worker must not be able to read a single
# byte from the files bucket, because it holds no GCS role at all.
# Duplicating that reasoning per-resource here (rather than "see k8s
# comment") is deliberate: this file is the one place someone auditing
# IAM would actually look.
#
# KSA <-> GSA names below MUST match the `iam.gke.io/gcp-service-account`
# annotations already committed in k8s/03-serviceaccount.yaml and
# k8s/16-worker-serviceaccounts.yaml — this module does not template
# those manifests (kubectl apply stays the deploy path for k8s/, per
# k8s/README.md; Terraform here stops at the GCP-resource boundary).
# After applying this module, replace those manifests' <PROJECT_ID>
# placeholder with var.project_id's real value before `kubectl apply`.

locals {
  # component -> (GSA account_id, KSA name, namespace) for the
  # Workload Identity bindings loop below.
  workload_identity_bindings = {
    app = {
      gsa = google_service_account.app.name
      ksa = "nimbusfs-ksa"
    }
    outbox_publisher = {
      gsa = google_service_account.outbox_publisher.name
      ksa = "nimbusfs-outbox-publisher-ksa"
    }
    file_worker = {
      gsa = google_service_account.file_worker.name
      ksa = "nimbusfs-file-worker-ksa"
    }
    thumbnail_worker = {
      gsa = google_service_account.thumbnail_worker.name
      ksa = "nimbusfs-thumbnail-worker-ksa"
    }
    notification_worker = {
      gsa = google_service_account.notification_worker.name
      ksa = "nimbusfs-notification-worker-ksa"
    }
    reconciliation = {
      gsa = google_service_account.reconciliation.name
      ksa = "nimbusfs-reconciliation-ksa"
    }
  }
}

# ===== nimbusfs-app (API Deployment, 03-serviceaccount.yaml) =====

resource "google_service_account" "app" {
  account_id   = "nimbusfs-app"
  display_name = "NimbusFS application (GKE Workload Identity)"
}

resource "google_storage_bucket_iam_member" "app_storage_admin" {
  count  = var.create_gcs_bucket ? 1 : 0
  bucket = google_storage_bucket.files[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

# Cloud SQL Auth Proxy path, if used — matches k8s/README.md exactly.
# Project-level because Cloud SQL has no per-instance IAM role scoping
# below "which instances in this project can this identity connect to
# at all" (that finer scoping is done via the DB's own user/password,
# which stays a Kubernetes Secret, not IAM).
resource "google_project_iam_member" "app_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# ===== nimbusfs-outbox-publisher =====
# Publisher on all 3 topics, no GCS role — per 16-worker-serviceaccounts.yaml's table.

resource "google_service_account" "outbox_publisher" {
  account_id   = "nimbusfs-outbox-publisher"
  display_name = "NimbusFS outbox publisher worker (Workload Identity)"
}

resource "google_pubsub_topic_iam_member" "outbox_publisher_file" {
  count  = var.create_pubsub_topics ? 1 : 0
  topic  = google_pubsub_topic.file_events[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.outbox_publisher.email}"
}

resource "google_pubsub_topic_iam_member" "outbox_publisher_upload" {
  count  = var.create_pubsub_topics ? 1 : 0
  topic  = google_pubsub_topic.upload_events[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.outbox_publisher.email}"
}

resource "google_pubsub_topic_iam_member" "outbox_publisher_notification" {
  count  = var.create_pubsub_topics ? 1 : 0
  topic  = google_pubsub_topic.notification_events[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.outbox_publisher.email}"
}

# ===== nimbusfs-file-worker =====
# Subscriber on file-worker-sub, publisher on file + notification
# topics (fans out thumbnail.requested and notification.requested
# worker-to-worker — app/workers/file_processing_worker.py), plus GCS
# objectViewer to verify uploaded bytes actually landed.

resource "google_service_account" "file_worker" {
  account_id   = "nimbusfs-file-worker"
  display_name = "NimbusFS file-processing worker (Workload Identity)"
}

resource "google_pubsub_subscription_iam_member" "file_worker_subscriber" {
  count        = var.create_pubsub_topics ? 1 : 0
  subscription = google_pubsub_subscription.file_worker[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.file_worker.email}"
}

resource "google_pubsub_topic_iam_member" "file_worker_publish_file" {
  count  = var.create_pubsub_topics ? 1 : 0
  topic  = google_pubsub_topic.file_events[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.file_worker.email}"
}

resource "google_pubsub_topic_iam_member" "file_worker_publish_notification" {
  count  = var.create_pubsub_topics ? 1 : 0
  topic  = google_pubsub_topic.notification_events[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.file_worker.email}"
}

resource "google_storage_bucket_iam_member" "file_worker_viewer" {
  count  = var.create_gcs_bucket ? 1 : 0
  bucket = google_storage_bucket.files[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.file_worker.email}"
}

# ===== nimbusfs-thumbnail-worker =====
# Subscriber on thumbnail-sub, objectViewer (read the source image) +
# objectCreator scoped to thumbnails/ ONLY via an IAM Condition — GCS
# has no native per-prefix role, so the condition expression is what
# actually enforces this. This is the one binding
# 16-worker-serviceaccounts.yaml calls out as "genuinely worth the
# extra clause": this worker decodes untrusted user-supplied bytes, so
# it is the most likely to be exploited and its write access most
# needs a ceiling.

resource "google_service_account" "thumbnail_worker" {
  account_id   = "nimbusfs-thumbnail-worker"
  display_name = "NimbusFS thumbnail worker (Workload Identity)"
}

resource "google_pubsub_subscription_iam_member" "thumbnail_worker_subscriber" {
  count        = var.create_pubsub_topics ? 1 : 0
  subscription = google_pubsub_subscription.thumbnail_worker[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.thumbnail_worker.email}"
}

resource "google_storage_bucket_iam_member" "thumbnail_worker_viewer" {
  count  = var.create_gcs_bucket ? 1 : 0
  bucket = google_storage_bucket.files[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.thumbnail_worker.email}"
}

resource "google_storage_bucket_iam_member" "thumbnail_worker_creator_scoped" {
  count  = var.create_gcs_bucket ? 1 : 0
  bucket = google_storage_bucket.files[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.thumbnail_worker.email}"

  condition {
    title       = "thumbnails-prefix-only"
    description = "Write access limited to the thumbnails/ prefix that app/services/thumbnail_service.py writes thumbnail_object_name to — the per-prefix scoping GCS has no native role for."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${local.gcs_bucket_name}/objects/thumbnails/\")"
  }
}

# ===== nimbusfs-notification-worker =====
# Subscriber on notification-sub ONLY. No GCS role at all — the
# blast-radius statement 16-worker-serviceaccounts.yaml leads with:
# this worker is the most exposed (the only one that will ever talk to
# a third party, per a future real email provider) and must not gain
# read access to every byte in the bucket as a side effect.

resource "google_service_account" "notification_worker" {
  account_id   = "nimbusfs-notification-worker"
  display_name = "NimbusFS notification worker (Workload Identity)"
}

resource "google_pubsub_subscription_iam_member" "notification_worker_subscriber" {
  count        = var.create_pubsub_topics ? 1 : 0
  subscription = google_pubsub_subscription.notification_worker[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.notification_worker.email}"
}

# ===== nimbusfs-reconciliation (Phase 9 CronJob) =====
# Read-only on both systems it inspects, matching
# app/services/reconciliation_service.py having no delete/update code
# path anywhere in its call graph. roles/storage.objectViewer (GCS) +
# roles/cloudsql.client (same DB network path the API uses) — NO write
# role on either. A future apply/quarantine mode would need to widen
# this GSA deliberately, by design (16-worker-serviceaccounts.yaml's
# closing comment).

resource "google_service_account" "reconciliation" {
  account_id   = "nimbusfs-reconciliation"
  display_name = "NimbusFS reconciliation CronJob (Workload Identity, read-only)"
}

resource "google_storage_bucket_iam_member" "reconciliation_viewer" {
  count  = var.create_gcs_bucket ? 1 : 0
  bucket = google_storage_bucket.files[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.reconciliation.email}"
}

resource "google_project_iam_member" "reconciliation_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.reconciliation.email}"
}

# ===== Workload Identity bindings (KSA -> GSA impersonation) =====
# One iam.workloadIdentityUser binding per GSA, scoped to exactly one
# namespace+KSA — matches k8s/README.md's single manual example,
# generalized to all 6 accounts via for_each instead of 6 near-copies.

resource "google_service_account_iam_member" "workload_identity" {
  for_each = local.workload_identity_bindings

  # each.value.gsa is the GSA's fully-qualified resource name
  # (projects/{project}/serviceAccounts/{email}), which also gives
  # Terraform the correct per-key implicit dependency — no explicit
  # depends_on needed.
  service_account_id = each.value.gsa
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.gke_namespace}/${each.value.ksa}]"
}
