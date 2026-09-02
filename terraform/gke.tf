# ---------------------------------------------------------------------
# GKE cluster + dedicated app node pool
# ---------------------------------------------------------------------
# Every flag here has a specific reason recorded in k8s/README.md's
# "One-time cluster setup" and main README.md §22 "GKE Cluster Design"
# — this file implements that documented design, it does not invent a
# new one. Notable choices carried over unchanged:
#   - regional (not zonal): control plane + node availability across
#     zones, which is what Phase 9's topologySpreadConstraints (07-
#     deployment.yaml, 18-21-deployment-*.yaml) actually needs a
#     multi-zone cluster underneath to mean anything.
#   - Dataplane V2 (enable_dataplane_v2): required for NetworkPolicy
#     enforcement (11-networkpolicy.yaml's default-deny policy is inert
#     without it).
#   - Workload Identity (workload_pool): the whole mechanism iam.tf's
#     bindings and 03/16-*-serviceaccount.yaml's KSA annotations depend
#     on — see those files' header comments for the full chain.
#   - private nodes + authorized networks: no node has a public IP;
#     kubectl access to the control plane is restricted to
#     var.master_authorized_networks (empty by default — apply will
#     succeed but you'll be locked out until you set it).

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.region

  # Standard Terraform GKE pattern: create the cluster with a throwaway
  # default node pool sized to zero, then manage all real capacity via
  # the dedicated google_container_node_pool below. Attempting to
  # manage the default pool's node count via this resource fights
  # Terraform on every subsequent apply.
  remove_default_node_pool = true
  initial_node_count       = 1

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.nodes.id

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "gke-pods"
    services_secondary_range_name = "gke-services"
  }

  datapath_provider = "ADVANCED_DATAPATH" # Dataplane V2

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false # allow access from authorized networks below, not ONLY from inside the VPC
    master_ipv4_cidr_block  = var.master_ipv4_cidr
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.master_authorized_networks
      content {
        cidr_block   = cidr_blocks.value.cidr_block
        display_name = cidr_blocks.value.display_name
      }
    }
  }

  # NimbusFS doesn't call the Kubernetes API from application code
  # today (03-serviceaccount.yaml's header comment says this
  # explicitly) — Cloud Monitoring/Logging integration is still worth
  # keeping on so kubectl logs/describe and the GKE dashboard work
  # without extra setup.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  deletion_protection = true

  lifecycle {
    ignore_changes = [
      node_config, # entirely delegated to the default-pool bootstrap; no real config lives on it
    ]
  }
}

resource "google_container_node_pool" "app_pool" {
  name     = "nimbusfs-app-pool"
  location = var.region
  cluster  = google_container_cluster.primary.name

  autoscaling {
    min_node_count = var.app_pool_min_nodes
    max_node_count = var.app_pool_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = var.app_pool_machine_type

    # GKE_METADATA is what actually turns on Workload Identity at the
    # node level — workload_identity_config on the cluster alone isn't
    # sufficient, both are required. Easy to apply this module, see no
    # error, and still have Pods fail to authenticate — this is the
    # flag most likely to be the reason if that happens.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Matches k8s/README.md's `--node-labels=pool=nimbusfs-app-pool`.
    # Not what 07-deployment.yaml's nodeAffinity actually matches on
    # (that uses the automatic cloud.google.com/gke-nodepool label,
    # satisfied by this node pool's `name` alone) — kept as a second,
    # explicit label anyway since the manual runbook set it and a
    # future manifest may prefer matching on it directly.
    labels = {
      pool = "nimbusfs-app-pool"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}
