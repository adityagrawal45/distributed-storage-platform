# ---------------------------------------------------------------------
# VPC — dedicated network, VPC-native subnet, Cloud NAT
# ---------------------------------------------------------------------
# Replaces k8s/README.md's `--network=default` shortcut with a real
# dedicated VPC (see variables.tf's network_name comment for why).
#
# Cloud NAT is required and was NOT mentioned in k8s/README.md's manual
# steps — it's a real gap the manual runbook has, filed here rather than
# silently fixed there. `--enable-private-nodes` (used by both the
# manual command and gke.tf below) means nodes have no public IPs, so
# without a NAT gateway they cannot pull the nimbusfs-api image from
# Artifact Registry, reach Pub/Sub, or reach GCS — everything in this
# app's dependency list. Private Google Access on the subnet covers
# Google APIs; Cloud NAT covers everything else (image registry pulls
# route through Google APIs too, but package installs, OS updates, and
# any non-Google egress do not).

resource "google_compute_network" "vpc" {
  name                    = var.network_name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "nodes" {
  name          = "${var.network_name}-nodes"
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.vpc.id

  # Required for GKE's --enable-ip-alias / VPC-native mode: Pods and
  # Services get real routable IPs from these secondary ranges instead
  # of relying on custom routes, which is also a prerequisite for
  # Dataplane V2 (k8s/README.md: "required for NetworkPolicy
  # enforcement — see 11-networkpolicy.yaml").
  secondary_ip_range {
    range_name    = "gke-pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "gke-services"
    ip_cidr_range = var.services_cidr
  }

  # Private nodes need this to reach Google APIs (Artifact Registry,
  # GCS, Pub/Sub, Cloud SQL Auth Proxy) without a public IP or without
  # routing through Cloud NAT for Google-owned destinations specifically.
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "router" {
  name    = "${var.network_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.network_name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# GKE's automatically-managed firewall rules already cover
# master-to-node and node-to-node traffic for a private cluster; this
# rule only adds what those don't: pods reaching each other and the
# node subnet across the secondary ranges, needed for the app's own
# in-cluster traffic (which 11-networkpolicy.yaml's default-deny policy
# then narrows further at the Kubernetes layer — this is the VPC-layer
# floor beneath it, not a replacement for it).
resource "google_compute_firewall" "allow_internal" {
  name    = "${var.network_name}-allow-internal"
  network = google_compute_network.vpc.id

  allow {
    protocol = "tcp"
  }
  allow {
    protocol = "udp"
  }
  allow {
    protocol = "icmp"
  }

  source_ranges = [
    var.subnet_cidr,
    var.pods_cidr,
    var.services_cidr,
  ]
}
