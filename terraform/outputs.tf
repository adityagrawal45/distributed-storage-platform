output "cluster_name" {
  value = google_container_cluster.primary.name
}

output "cluster_endpoint" {
  description = "Private control-plane endpoint. Reach it via `gcloud container clusters get-credentials` from a network in master_authorized_networks."
  value       = google_container_cluster.primary.endpoint
  sensitive   = true
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.primary.name} --region ${var.region} --project ${var.project_id}"
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.nimbusfs.repository_id}"
}

output "ingress_static_ip" {
  description = "Point api.nimbusfs.example.com's A record here, then update k8s/14-managedcertificate.yaml and k8s/15-ingress.yaml to match, per k8s/README.md."
  value       = google_compute_global_address.ingress_ip.address
}

output "gcs_bucket_name" {
  value = var.create_gcs_bucket ? google_storage_bucket.files[0].name : null
}

output "service_account_emails" {
  description = "The 6 GSA emails — paste these into the matching `iam.gke.io/gcp-service-account` annotations in k8s/03-serviceaccount.yaml and k8s/16-worker-serviceaccounts.yaml (replacing the <PROJECT_ID> placeholder) before `kubectl apply`."
  value = {
    app                 = google_service_account.app.email
    outbox_publisher    = google_service_account.outbox_publisher.email
    file_worker         = google_service_account.file_worker.email
    thumbnail_worker    = google_service_account.thumbnail_worker.email
    notification_worker = google_service_account.notification_worker.email
    reconciliation      = google_service_account.reconciliation.email
  }
}

output "next_steps" {
  value = <<-EOT
    1. Run the get_credentials_command output above.
    2. Fill in each of the 6 `iam.gke.io/gcp-service-account` annotations
       in k8s/03-serviceaccount.yaml and k8s/16-worker-serviceaccounts.yaml
       using the service_account_emails output above.
    3. Cloud SQL + Memorystore are NOT provisioned by this module
       (deliberately out of scope for this pass) — follow
       k8s/README.md's "Cloud SQL & Memorystore" section, then update
       05-configmap.yaml's POSTGRES_HOST/REDIS_HOST and
       11-networkpolicy.yaml's placeholder CIDRs.
    4. Create the Kubernetes Secret per k8s/README.md's "Secrets setup"
       (never commit it).
    5. Point DNS at the ingress_static_ip output, update
       14-managedcertificate.yaml / 15-ingress.yaml's domain.
    6. Build & push the image (k8s/README.md "Build & push the image"),
       using artifact_registry_repo above as the registry.
    7. ./scripts/k8s-deploy.sh
  EOT
}
