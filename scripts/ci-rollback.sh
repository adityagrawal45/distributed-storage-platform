#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Phase 12 — non-interactive rollback, driven by CI (called
# automatically by deploy-staging.yml/deploy-production.yml when
# scripts/k8s-smoke-test.sh fails after a deploy) or manually via
# `gh workflow run rollback.yml`.
#
# Uses `kubectl rollout undo` — i.e. "go back to the previous
# ReplicaSet revision Kubernetes already has recorded," NOT "redeploy
# an older image tag." This matters: `rollout undo` is instant (the
# old ReplicaSet's Pods already exist or spin up from an already-cached
# image) where re-running ci-deploy.sh with an old IMAGE_TAG would
# repeat the full apply+rollout+wait sequence — slower, and it
# re-triggers the exact rollout mechanics that may have just failed.
#
# Rolls back every Deployment together, not just the API — a bad
# release is usually one image tag across all of them (same image,
# different entrypoint per CONTEXT.md), so rolling back only the API
# while workers keep running the new tag would leave the fleet on two
# inconsistent versions.
# ---------------------------------------------------------------------
set -euo pipefail

FAILED=0
for deployment in nimbusfs-api nimbusfs-outbox-publisher nimbusfs-file-worker nimbusfs-thumbnail-worker nimbusfs-notification-worker; do
    echo "Rolling back $deployment..."
    if kubectl rollout undo "deployment/$deployment" -n nimbusfs; then
        if ! kubectl rollout status "deployment/$deployment" -n nimbusfs --timeout=300s; then
            echo "  [FAIL] $deployment did not reach a healthy state after rollback."
            FAILED=1
        fi
    else
        echo "  [FAIL] rollout undo failed for $deployment (no previous revision? see 'kubectl rollout history')."
        FAILED=1
    fi
done

if [[ "$FAILED" -ne 0 ]]; then
    echo ""
    echo "One or more rollbacks did not complete cleanly — this needs a human, now."
    echo "See docs/rollback.md 'When automated rollback itself fails'."
    exit 1
fi

echo ""
echo "Rollback complete. Run scripts/k8s-smoke-test.sh to confirm health."
