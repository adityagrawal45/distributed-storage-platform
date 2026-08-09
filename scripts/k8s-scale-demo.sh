#!/usr/bin/env bash
# Demonstrates the HPA scaling nimbusfs-api from its floor (3) toward
# its ceiling (10) under synthetic CPU load, then back down once load
# stops — see k8s/09-hpa.yaml for the thresholds/behavior this exercises.
#
# Generates load with a disposable in-cluster Pod hammering the
# ClusterIP Service directly (bypassing the Ingress/GCLB, which isn't
# needed to load-test the HPA's CPU metric) — no external tooling
# required.
set -euo pipefail

NAMESPACE="nimbusfs"
LOAD_POD="nimbusfs-load-generator"

cleanup() {
    echo ""
    echo "Cleaning up load generator..."
    kubectl delete pod "$LOAD_POD" -n "$NAMESPACE" --ignore-not-found=true --wait=false
}
trap cleanup EXIT

echo "Current HPA state:"
kubectl get hpa nimbusfs-api-hpa -n "$NAMESPACE"

echo ""
echo "Starting synthetic load (Ctrl+C to stop early and observe scale-down)..."
kubectl run "$LOAD_POD" \
    --namespace "$NAMESPACE" \
    --image=busybox:1.36 \
    --restart=Never \
    -- /bin/sh -c "while true; do wget -q -O- http://nimbusfs-api.${NAMESPACE}.svc.cluster.local/api/v1/live > /dev/null; done"

echo "Load generator running. Watching HPA + replica count for up to 5 minutes"
echo "(scale-up has a 0s stabilization window — see k8s/09-hpa.yaml — so this"
echo "should start climbing within the first ~60-90s once CPU crosses 70%):"
echo ""
for i in $(seq 1 30); do
    sleep 10
    kubectl get hpa nimbusfs-api-hpa -n "$NAMESPACE" --no-headers
done

echo ""
echo "Stopping load generator..."
kubectl delete pod "$LOAD_POD" -n "$NAMESPACE" --ignore-not-found=true --wait=false
trap - EXIT

echo ""
echo "Load stopped. Scale-down has a 300s stabilization window (k8s/09-hpa.yaml)"
echo "— replicas will drift back toward 3 gradually, not immediately. Watch with:"
echo "  kubectl get hpa nimbusfs-api-hpa -n $NAMESPACE --watch"
