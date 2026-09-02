# ---------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------
# Defaults mirror k8s/README.md's documented values exactly (PROJECT_ID,
# REGION=us-central1, CLUSTER_NAME=nimbusfs-cluster, master CIDR
# 172.16.0.0/28, node pool nimbusfs-app-pool, e2-standard-4, 1-6 nodes)
# so applying this module produces the same cluster the manual runbook
# describes, not a divergent one.

variable "project_id" {
  description = "GCP project ID to deploy NimbusFS infrastructure into. Must NOT be an unrelated pre-existing project (this module was written after finding `gcloud config` pointed at an unrelated project — verify with `gcloud config get-value project` before applying)."
  type        = string
}

variable "region" {
  description = "GCP region for the regional GKE cluster, subnet, and Artifact Registry repo."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Short environment tag (dev/staging/prod) — appended to resource names that need to differ per environment (e.g. the GCS bucket, which k8s/README.md already names nimbusfs-files-dev vs nimbusfs-files-prod)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "cluster_name" {
  description = "GKE cluster name."
  type        = string
  default     = "nimbusfs-cluster"
}

# --- Networking ---

variable "network_name" {
  description = "VPC name. A dedicated VPC, not `default` — the manual runbook used `default` as a shortcut; this module doesn't, since a shared default VPC is exactly the kind of blast-radius mistake Phase 8's per-worker-GSA reasoning (k8s/16-worker-serviceaccounts.yaml) argues against at the IAM layer. Doing the same at the network layer is the same principle."
  type        = string
  default     = "nimbusfs-vpc"
}

variable "subnet_cidr" {
  description = "Primary CIDR range for the GKE nodes subnet."
  type        = string
  default     = "10.10.0.0/20" # 4094 usable node IPs
}

variable "pods_cidr" {
  description = "Secondary range CIDR for Pod IPs (VPC-native / --enable-ip-alias)."
  type        = string
  default     = "10.20.0.0/14" # ~262k Pod IPs — generous headroom for HPA 3->10 plus 4 worker Deployments plus future growth
}

variable "services_cidr" {
  description = "Secondary range CIDR for Service (ClusterIP) IPs."
  type        = string
  default     = "10.30.0.0/20"
}

variable "master_ipv4_cidr" {
  description = "CIDR for the GKE control plane's private endpoint. Must be a /28 not overlapping any other range in the VPC."
  type        = string
  default     = "172.16.0.0/28" # matches k8s/README.md exactly
}

variable "master_authorized_networks" {
  description = "CIDR blocks allowed to reach the private GKE control plane endpoint (kubectl access). Empty by default — you MUST set this (your office/VPN CIDR, or a bastion's IP) before apply, or nothing will be able to reach the API server. This is deliberately not defaulted to 0.0.0.0/0."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

# --- Node pool ---

variable "app_pool_machine_type" {
  description = "Machine type for the nimbusfs-app-pool node pool. 07-deployment.yaml's nodeAffinity prefers scheduling onto a pool named exactly nimbusfs-app-pool (via the automatic cloud.google.com/gke-nodepool label) — this module's node pool name must keep matching that, not this machine type, which is safe to change."
  type        = string
  default     = "e2-standard-4"
}

variable "app_pool_min_nodes" {
  type    = number
  default = 1
}

variable "app_pool_max_nodes" {
  type    = number
  default = 6
}

# --- IAM / Workload Identity ---

variable "gke_namespace" {
  description = "Kubernetes namespace the workload-identity-bound KSAs live in. Must match k8s/00-namespace.yaml."
  type        = string
  default     = "nimbusfs"
}

# --- Supporting resources IAM needs something real to scope roles to ---
# (GKE + IAM without them would mean granting project-wide roles, which
# is exactly the blast-radius mistake k8s/16-worker-serviceaccounts.yaml
# argues against — so this module creates the GCS bucket and Pub/Sub
# topics/subscriptions IAM already assumes exist, gated behind toggles
# so a later Terraform iteration that manages them differently — e.g.
# alongside Cloud SQL/Memorystore, explicitly out of scope for this
# pass — can flip these off without this module fighting it.)

variable "create_gcs_bucket" {
  description = "Whether this module creates the application GCS bucket (used to scope nimbusfs-app's storage.objectAdmin IAM binding). Set false if the bucket is already managed elsewhere."
  type        = bool
  default     = true
}

variable "gcs_bucket_name" {
  description = "Application GCS bucket name. Defaults match app/core/config/settings.py's GCS_BUCKET_NAME pattern (nimbusfs-files-<env>)."
  type        = string
  default     = "" # computed in storage.tf as nimbusfs-files-${var.environment} when empty
}

variable "gcs_bucket_location" {
  description = "GCS bucket location. Regional (not dual/multi-region) — matches docs/high-availability.md's explicit, cost-aware rejection of a dual-region bucket in favor of a regional bucket + scheduled cross-region replication job."
  type        = string
  default     = "US-CENTRAL1"
}

variable "create_pubsub_topics" {
  description = "Whether this module creates the 3 Phase 8 Pub/Sub topics and their subscriptions (used to scope per-worker publisher/subscriber IAM bindings, matching k8s/16-worker-serviceaccounts.yaml's table exactly). Set false if messaging infra is already managed elsewhere."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Common labels applied to created resources."
  type        = map(string)
  default = {
    app          = "nimbusfs"
    "managed-by" = "terraform"
  }
}
