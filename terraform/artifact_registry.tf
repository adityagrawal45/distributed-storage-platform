# ---------------------------------------------------------------------
# Artifact Registry — where docker/Dockerfile's built image is pushed
# ---------------------------------------------------------------------
# Matches k8s/README.md's "Build & push the image" section exactly
# (`gcloud artifacts repositories create nimbusfs --repository-format=
# docker --location="$REGION"`). 07-deployment.yaml's `image:` field and
# 18-21-deployment-*.yaml (same image, different entrypoint per
# CONTEXT.md) all pull from this repo.

resource "google_artifact_registry_repository" "nimbusfs" {
  repository_id = "nimbusfs"
  location      = var.region
  format        = "DOCKER"
  description   = "NimbusFS API + worker images (single image, different `python -m app.workers.<name>` entrypoint per k8s/16-21 Deployments)"
  labels        = var.labels
}

# Static IP for the GKE Ingress (14-managedcertificate.yaml,
# 15-ingress.yaml) — matches k8s/README.md's "DNS & static IP" section.
resource "google_compute_global_address" "ingress_ip" {
  name = "nimbusfs-ip"
}
