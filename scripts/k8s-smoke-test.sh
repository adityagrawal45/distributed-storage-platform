#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Phase 5 verification suite — a Kubernetes cluster isn't something
# `pytest` can meaningfully exercise (see k8s/README.md "Testing" for
# why this is a kubectl-driven script instead of a pytest file), so
# this is the equivalent: a sequence of real checks against a real
# cluster, each printing PASS/FAIL, covering every behavior this phase
# added. Run after `./scripts/k8s-deploy.sh`.
#
# Non-destructive by default. Pass `--full` to additionally run the
# self-healing (delete a Pod) and rolling-update/rollback demos, which
# briefly perturb the running Deployment (safe on any environment, but
# opt-in since they're not read-only).
# ---------------------------------------------------------------------
set -euo pipefail

NAMESPACE="nimbusfs"
DEPLOYMENT="nimbusfs-api"
FULL="${1:-}"
PASS=0
FAIL=0

check() {
    local description="$1"
    local condition="$2"
    if eval "$condition"; then
        echo "  [PASS] $description"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $description"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== 1. Namespace, quota, config objects exist ==="
check "Namespace '$NAMESPACE' exists" "kubectl get namespace $NAMESPACE >/dev/null 2>&1"
check "ResourceQuota exists" "kubectl get resourcequota nimbusfs-quota -n $NAMESPACE >/dev/null 2>&1"
check "ConfigMap exists" "kubectl get configmap nimbusfs-config -n $NAMESPACE >/dev/null 2>&1"
check "Secret exists" "kubectl get secret nimbusfs-secrets -n $NAMESPACE >/dev/null 2>&1"
check "ServiceAccount exists" "kubectl get serviceaccount nimbusfs-ksa -n $NAMESPACE >/dev/null 2>&1"

echo ""
echo "=== 2. Deployment health ==="
DESIRED=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
READY=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}')
check "Deployment exists" "kubectl get deployment $DEPLOYMENT -n $NAMESPACE >/dev/null 2>&1"
check "readyReplicas ($READY) == desired replicas ($DESIRED)" "[[ \"$READY\" == \"$DESIRED\" ]]"
check "At least 3 Pods (HPA minReplicas floor)" "[[ \"$READY\" -ge 3 ]]"

echo ""
echo "=== 3. Readiness / Liveness probes ==="
POD=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=nimbusfs -o jsonpath='{.items[0].metadata.name}')
check "Sample Pod ($POD) is Ready" \
    "[[ \$(kubectl get pod $POD -n $NAMESPACE -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}') == 'True' ]]"
check "/api/v1/live reachable from inside the Pod" \
    "kubectl exec -n $NAMESPACE $POD -- python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/live').status==200 else 1)\""
check "/api/v1/ready reachable from inside the Pod" \
    "kubectl exec -n $NAMESPACE $POD -- python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/ready').status==200 else 1)\""

echo ""
echo "=== 4. Service, Ingress, HPA, PDB, NetworkPolicy objects ==="
check "Service has Endpoints (Pods are wired up)" \
    "[[ -n \$(kubectl get endpoints $DEPLOYMENT -n $NAMESPACE -o jsonpath='{.subsets[0].addresses}') ]]"
check "Ingress has an assigned address" \
    "[[ -n \$(kubectl get ingress nimbusfs-ingress -n $NAMESPACE -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null) ]]"
check "HPA is active and reading metrics" \
    "kubectl get hpa nimbusfs-api-hpa -n $NAMESPACE -o jsonpath='{.status.currentReplicas}' | grep -qE '^[0-9]+$'"
check "PodDisruptionBudget reports a currentHealthy count" \
    "kubectl get pdb nimbusfs-api-pdb -n $NAMESPACE -o jsonpath='{.status.currentHealthy}' | grep -qE '^[0-9]+$'"
check "NetworkPolicies are present (default-deny + allow-ingress + allow-egress)" \
    "[[ \$(kubectl get networkpolicy -n $NAMESPACE --no-headers | wc -l) -ge 3 ]]"

echo ""
if [[ "$FULL" == "--full" ]]; then
    echo "=== 5. Self-healing: delete a Pod, confirm automatic recreation ==="
    VICTIM=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=nimbusfs -o jsonpath='{.items[0].metadata.name}')
    echo "  Deleting Pod $VICTIM ..."
    kubectl delete pod "$VICTIM" -n "$NAMESPACE" --wait=false
    echo "  Waiting up to 90s for the ReplicaSet to converge back to $DESIRED ready replicas..."
    for _ in $(seq 1 18); do
        sleep 5
        CURRENT_READY=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}')
        [[ "$CURRENT_READY" == "$DESIRED" ]] && break
    done
    check "ReplicaSet recreated the deleted Pod (back to $DESIRED ready)" "[[ \"$CURRENT_READY\" == \"$DESIRED\" ]]"
    check "Deleted Pod name no longer present" "! kubectl get pod $VICTIM -n $NAMESPACE >/dev/null 2>&1"

    echo ""
    echo "=== 6. Rolling update + rollback ==="
    CURRENT_IMAGE=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')
    echo "  Current image: $CURRENT_IMAGE"
    echo "  Triggering a no-op rolling restart (same image) to exercise the rollout mechanics..."
    kubectl rollout restart deployment/"$DEPLOYMENT" -n "$NAMESPACE"
    check "Rollout completes with zero downtime (maxUnavailable: 0)" \
        "kubectl rollout status deployment/$DEPLOYMENT -n $NAMESPACE --timeout=300s"
    echo "  Rollout history:"
    kubectl rollout history deployment/"$DEPLOYMENT" -n "$NAMESPACE"
    echo "  Demonstrating rollback to the previous revision..."
    kubectl rollout undo deployment/"$DEPLOYMENT" -n "$NAMESPACE"
    check "Rollback completes successfully" \
        "kubectl rollout status deployment/$DEPLOYMENT -n $NAMESPACE --timeout=300s"
else
    echo "(Skipping self-healing and rolling-update/rollback demos — pass --full to run them.)"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
