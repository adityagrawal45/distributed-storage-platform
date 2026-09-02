# ---------------------------------------------------------------------
# Pub/Sub topics + subscriptions (Phase 8) — created only so IAM has
# real resources to scope per-worker roles to, per variables.tf's
# create_pubsub_topics comment.
# ---------------------------------------------------------------------
# Names match app/core/config/settings.py's defaults EXACTLY
# (FILE_EVENTS_TOPIC, UPLOAD_EVENTS_TOPIC, NOTIFICATION_EVENTS_TOPIC,
# FILE_WORKER_SUBSCRIPTION, THUMBNAIL_WORKER_SUBSCRIPTION,
# NOTIFICATION_WORKER_SUBSCRIPTION) — three topics, not one firehose or
# twelve per-event-type topics, per app/events/topics.py's module
# docstring. If you change these in settings.py for a real deployment,
# change them here too; nothing cross-checks the two at apply time.
#
# PUBSUB_ENABLED defaults to false in settings.py, so provisioning
# these costs nothing at rest and the app runs unaffected whether or
# not var.create_pubsub_topics is true — this only matters once
# PUBSUB_ENABLED is flipped on for a real deployment.

locals {
  file_events_topic         = "nimbusfs-file-events"
  upload_events_topic       = "nimbusfs-upload-events"
  notification_events_topic = "nimbusfs-notification-events"
}

resource "google_pubsub_topic" "file_events" {
  count  = var.create_pubsub_topics ? 1 : 0
  name   = local.file_events_topic
  labels = var.labels
}

resource "google_pubsub_topic" "upload_events" {
  count  = var.create_pubsub_topics ? 1 : 0
  name   = local.upload_events_topic
  labels = var.labels
}

resource "google_pubsub_topic" "notification_events" {
  count  = var.create_pubsub_topics ? 1 : 0
  name   = local.notification_events_topic
  labels = var.labels
}

# --- Subscriptions ---
# file-worker and thumbnail-worker both subscribe to file_events (with
# separate subscriptions, so each gets its own independent backlog/ack
# cursor — a slow thumbnail worker never delays file-processing
# delivery or vice versa); notification-worker subscribes to its own
# isolated topic entirely (app/events/topics.py: "a wedged notification
# backlog ... can never apply backpressure to file processing").

resource "google_pubsub_subscription" "file_worker" {
  count                        = var.create_pubsub_topics ? 1 : 0
  name                         = "nimbusfs-file-events-file-worker-sub"
  topic                        = google_pubsub_topic.file_events[0].id
  ack_deadline_seconds         = 60    # matches settings.py's PUBSUB_ACK_DEADLINE
  enable_exactly_once_delivery = false # effectively-once is handled at the app layer via ProcessedEvent's unique constraint — see app/workers/base.py
  labels                       = var.labels
}

resource "google_pubsub_subscription" "thumbnail_worker" {
  count                = var.create_pubsub_topics ? 1 : 0
  name                 = "nimbusfs-file-events-thumbnail-worker-sub"
  topic                = google_pubsub_topic.file_events[0].id
  ack_deadline_seconds = 60
  labels               = var.labels
}

resource "google_pubsub_subscription" "notification_worker" {
  count                = var.create_pubsub_topics ? 1 : 0
  name                 = "nimbusfs-notification-events-notification-worker-sub"
  topic                = google_pubsub_topic.notification_events[0].id
  ack_deadline_seconds = 60
  labels               = var.labels
}
