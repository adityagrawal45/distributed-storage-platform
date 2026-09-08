#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Phase 12 — non-interactive deploy, driven by CI (deploy-staging.yml /
# deploy-production.yml). NOT a replacement for scripts/k8s-deploy.sh,
# which stays the human-run entrypoint (interactive confirmation
# prompt, applies against whatever context `kubectl` is already
# pointed at). This script exists because CI needs three things that
# script deliberately doesn't have:
#
#   1. No interactive prompt (`set -euo pipefail` + no `read`) — a
#      workflow run has no terminal to answer one.
#   2. PROJECT_ID/REGION/IMAGE_TAG substitution — k8s/*.yaml ships with
#      literal `<PROJECT_ID>`/`PROJECT_ID`/`REGION`/`:v1.0.0` placeholders
#      (see k8s/README.md's own manual "replace the placeholder" step);
#      CI fills them in from repo variables instead of a human editing
#      files by hand before every deploy.
#   3. Applies EVERY manifest, including the Phase 8/9 worker
#      Deployments + CronJob (16-23) and the Phase 11 PodMonitoring
#      (24) — a real gap in scripts/k8s-deploy.sh (it only ever applied
#      00-15), left uncorrected there deliberately: fixing a human
#      runbook script's scope is a separate, smaller change than
#      standing up CI, and this script's manifest list is now the
#      single source of truth going forward (see docs/ci-cd.md
#      "Known gap carried forward").
#
# Required environment variables: PROJECT_ID, REGION, IMAGE_TAG.
# Assumes `kubectl` is already authenticated against the target
# cluster (deploy-staging.yml/deploy-production.yml do this via
# `google-github-actions/get-gke-credentials` immediately before
# calling this script) and that the `nimbusfs-secrets` Kubernetes
# Secret already exists in-cluster (created out-of-band per
# k8s/README.md's "Secrets setup" — CI never creates, reads, or
# templates real secret VALUES, only references the Secret object by
# name, exactly like scripts/k8s-deploy.sh already does).
# ---------------------------------------------------------------------
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID must be set}"
: "${REGION:?REGION must be set}"
: "${IMAGE_TAG:?IMAGE_TAG must be set}"

cd "$(dirname "$0")/.."

if ! kubectl get secret nimbusfs-secrets -n nimbusfs >/dev/null 2>&1; then
    echo "ERROR: Secret 'nimbusfs-secrets' does not exist in namespace 'nimbusfs'."
    echo "CI never creates it — see k8s/README.md 'Secrets setup'. Create it once, out-of-band, then re-run."
    exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Rendering manifests -> $WORKDIR (PROJECT_ID=$PROJECT_ID REGION=$REGION IMAGE_TAG=$IMAGE_TAG)"
for f in k8s/*.yaml; do
    base="$(basename "$f")"
    sed \
        -e "s/<PROJECT_ID>/${PROJECT_ID}/g" \
        -e "s/REGION-docker\.pkg\.dev\/PROJECT_ID/${REGION}-docker.pkg.dev\/${PROJECT_ID}/g" \
        -e "s/nimbusfs-api:v1\.0\.0/nimbusfs-api:${IMAGE_TAG}/g" \
        "$f" > "$WORKDIR/$base"
done
# 06-secret.example.yaml is a TEMPLATE for a human to fill in locally —
# never applied by anything automated, including this script.
rm -f "$WORKDIR/06-secret.example.yaml"

echo "Applying manifests (numeric filename order = correct dependency order)..."
kubectl apply -f "$WORKDIR"

echo ""
echo "Waiting for rollouts..."
for deployment in nimbusfs-api nimbusfs-outbox-publisher nimbusfs-file-worker nimbusfs-thumbnail-worker nimbusfs-notification-worker; do
    echo "  -> $deployment"
    kubectl rollout status "deployment/$deployment" -n nimbusfs --timeout=300s
done

echo ""
echo "Deployed image tag: $IMAGE_TAG"
echo "Done. Next: scripts/k8s-smoke-test.sh (read-only verification)."
