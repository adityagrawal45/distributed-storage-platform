# ---------------------------------------------------------------------
# Cloud Monitoring alert policies + uptime check (Phase 11)
# ---------------------------------------------------------------------
# See docs/alerting.md for the full alert catalog, thresholds, and the
# reasoning behind each duration/severity. See docs/monitoring.md §1 for
# why this is Cloud Monitoring alert policies, not a self-hosted
# Alertmanager: NimbusFS runs no self-hosted Prometheus (see
# app/core/metrics.py's module docstring), so there is nothing for
# Alertmanager to sit in front of — Cloud Monitoring alert policies are
# the only alerting layer that exists here, reading the SAME metrics
# Google Managed Prometheus ingests from `/metrics`
# (k8s/24-podmonitoring.yaml) alongside GKE/Cloud SQL/Memorystore's own
# native metrics.
#
# Gated behind `var.create_monitoring_alerts` (default false) — see that
# variable's docstring in variables.tf. NOT run through `terraform
# validate`/`plan` this session (no `terraform` binary was installed) —
# reviewed by eye against the google_monitoring_alert_policy /
# google_monitoring_uptime_check_config resource schemas only. Run
# `terraform validate` before relying on this file; it carries a lower
# confidence level than the rest of this module, which WAS validated
# during Phase 9's Terraform extension.

resource "google_monitoring_notification_channel" "email" {
  count        = var.create_monitoring_alerts && var.alert_notification_email != "" ? 1 : 0
  display_name = "NimbusFS on-call email"
  type         = "email"
  labels = {
    email_address = var.alert_notification_email
  }
}

locals {
  notification_channels = var.create_monitoring_alerts && var.alert_notification_email != "" ? [
    google_monitoring_notification_channel.email[0].id
  ] : []
}

# -----------------------------------------------------------------
# CRITICAL: sustained 5xx rate — "API unavailable" (docs/alerting.md §2)
# -----------------------------------------------------------------
resource "google_monitoring_alert_policy" "api_unavailable" {
  count        = var.create_monitoring_alerts ? 1 : 0
  display_name = "NimbusFS: API unavailable (5xx > 50%)"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "5xx ratio > 50% for 2m"
    condition_threshold {
      # Filters on the Prometheus metric Google Managed Prometheus
      # ingests from GET /metrics — see app/core/metrics.py's
      # HTTP_REQUESTS_TOTAL definition. The exact GMP metric-type prefix
      # (prometheus.googleapis.com/nimbusfs_http_requests_total/counter)
      # is GMP's documented naming convention for scraped Prometheus
      # counters.
      filter          = "metric.type=\"prometheus.googleapis.com/nimbusfs_http_requests_total/counter\" AND resource.type=\"prometheus_target\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.5
      duration        = "120s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.status_code"]
      }
    }
  }

  notification_channels = local.notification_channels
  documentation {
    content   = "See docs/alerting.md \"API unavailable\" and docs/incident-response.md §3. Check nimbusfs_http_requests_total by route/status_code first."
    mime_type = "text/markdown"
  }
}

# -----------------------------------------------------------------
# HIGH: sustained elevated error rate (docs/alerting.md §2)
# -----------------------------------------------------------------
resource "google_monitoring_alert_policy" "high_error_rate" {
  count        = var.create_monitoring_alerts ? 1 : 0
  display_name = "NimbusFS: sustained high error rate (5xx > 5%)"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "5xx ratio > 5% for 5m"
    condition_threshold {
      filter          = "metric.type=\"prometheus.googleapis.com/nimbusfs_http_requests_total/counter\" AND resource.type=\"prometheus_target\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.05
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.route"]
      }
    }
  }

  notification_channels = local.notification_channels
  documentation {
    content   = "See docs/alerting.md \"Sustained high error rate\" and docs/incident-response.md §3."
    mime_type = "text/markdown"
  }
}

# -----------------------------------------------------------------
# HIGH: worker permanent-failure rate (docs/alerting.md §2)
# -----------------------------------------------------------------
resource "google_monitoring_alert_policy" "worker_failure_rate" {
  count        = var.create_monitoring_alerts ? 1 : 0
  display_name = "NimbusFS: worker processing failures > 25%"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "result=failed ratio > 25% for 5m, any consumer"
    condition_threshold {
      filter          = "metric.type=\"prometheus.googleapis.com/nimbusfs_pubsub_messages_processed_total/counter\" AND resource.type=\"prometheus_target\" AND metric.label.result=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.25
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.consumer"]
      }
    }
  }

  notification_channels = local.notification_channels
  documentation {
    content   = "See docs/alerting.md \"Severe data-processing failure\" and docs/incident-response.md §4."
    mime_type = "text/markdown"
  }
}

# -----------------------------------------------------------------
# Uptime check — GET /api/v1/live (docs/alerting.md §4)
# -----------------------------------------------------------------
resource "google_monitoring_uptime_check_config" "live" {
  count        = var.create_monitoring_alerts && var.uptime_check_host != "" ? 1 : 0
  display_name = "NimbusFS /api/v1/live"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/api/v1/live"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.uptime_check_host
    }
  }
}
