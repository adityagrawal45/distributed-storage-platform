#!/usr/bin/env bash
# Applies every NimbusFS Kubernetes manifest, in the correct order, to
# whatever cluster the current kubectl context points at.
#
# Prerequisites (see k8s/README.md for the full one-time setup):
#   - `gcloud container clusters get-credentials <cluster> --region <region>`
#     already run, so `kubectl config current-context` is the target GKE cluster.
#   - k8s/06-secret.yaml exists locally (copied from 06-secret.example.yaml
#     and filled in) OR the `nimbusfs-secrets` Secret was already created
#     imperatively — see k8s/06-secret.example.yaml's header comment.
#   - The image referenced in k8s/07-deployment.yaml has been pushed to
#     Artifact Registry.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Target context: $(kubectl config current-context)"
read -r -p "Deploy NimbusFS to this context? [y/N] " confirm
if [[ "${confirm:-N}" != "y" && "${confirm:-N}" != "Y" ]]; then
    echo "Aborted."
    exit 1
fi

if [[ ! -f k8s/06-secret.yaml ]]; then
    if ! kubectl get secret nimbusfs-secrets -n nimbusfs >/dev/null 2>&1; then
        echo "ERROR: k8s/06-secret.yaml is missing AND no 'nimbusfs-secrets' Secret"
        echo "exists in the cluster yet. Create it first — see"
        echo "k8s/06-secret.example.yaml's header comment for both options."
        exit 1
    fi
    echo "Using existing in-cluster 'nimbusfs-secrets' Secret (no local k8s/06-secret.yaml found)."
fi

# kubectl applies files in a directory in filename-sorted order, which
# is exactly why every manifest is numerically prefixed — Namespace
# before anything that lives in it, ConfigMap/Secret before the
# Deployment that references them, etc.
echo "Applying manifests..."
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-resourcequota.yaml
kubectl apply -f k8s/02-limitrange.yaml
kubectl apply -f k8s/03-serviceaccount.yaml
kubectl apply -f k8s/04-rbac.yaml
kubectl apply -f k8s/05-configmap.yaml

if [[ -f k8s/06-secret.yaml ]]; then
    kubectl apply -f k8s/06-secret.yaml
fi

kubectl apply -f k8s/07-deployment.yaml
kubectl apply -f k8s/08-service.yaml
kubectl apply -f k8s/09-hpa.yaml
kubectl apply -f k8s/10-pdb.yaml
kubectl apply -f k8s/11-networkpolicy.yaml
kubectl apply -f k8s/12-backendconfig.yaml
kubectl apply -f k8s/13-frontendconfig.yaml
kubectl apply -f k8s/14-managedcertificate.yaml
kubectl apply -f k8s/15-ingress.yaml

echo ""
echo "Applied. Waiting for the rollout to finish (up to 5 minutes)..."
kubectl rollout status deployment/nimbusfs-api -n nimbusfs --timeout=300s

echo ""
echo "Done. Useful next commands:"
echo "  kubectl get pods -n nimbusfs -o wide"
echo "  kubectl get hpa -n nimbusfs"
echo "  kubectl describe managedcertificate nimbusfs-cert -n nimbusfs   # check TLS provisioning status"
echo "  ./scripts/k8s-smoke-test.sh"
