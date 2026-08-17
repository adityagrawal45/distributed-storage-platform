# NimbusFS on GKE — Deployment Runbook

Operational companion to the main [README.md](../README.md) §21+ (Phase 5
narrative/design). This file is the step-by-step "how do I actually get
a cluster running" guide. Every manifest here is heavily commented
in-place — read the file itself for the *why* behind each field; this
README covers setup steps that live outside any single YAML file.

## Manifest order

Files are numerically prefixed because `kubectl apply -f k8s/` applies
a directory's files in filename-sorted order, and dependency order
matters here (Namespace before anything in it, ConfigMap/Secret before
the Deployment that mounts them, etc.). `scripts/k8s-deploy.sh` applies
them in this same order explicitly, so it works even against `kubectl`
versions/configurations where directory-apply ordering isn't guaranteed.

| File | Object(s) |
|---|---|
| `00-namespace.yaml` | `nimbusfs` Namespace (Pod Security "restricted") |
| `01-resourcequota.yaml` | Namespace-wide resource ceiling |
| `02-limitrange.yaml` | Per-container resource guardrails/defaults |
| `03-serviceaccount.yaml` | KSA bound to a GCP IAM SA via Workload Identity |
| `04-rbac.yaml` | Least-privilege Role + RoleBinding |
| `05-configmap.yaml` | Non-secret application configuration |
| `06-secret.example.yaml` | **Template only** — see "Secrets Setup" below |
| `07-deployment.yaml` | The API Deployment: probes, resources, affinity, rolling strategy |
| `08-service.yaml` | ClusterIP Service + container-native LB annotations |
| `09-hpa.yaml` | HorizontalPodAutoscaler (3 → 10 replicas) |
| `10-pdb.yaml` | PodDisruptionBudget (`minAvailable: 2`) |
| `11-networkpolicy.yaml` | Default-deny + explicit allow-lists |
| `12-backendconfig.yaml` | GCLB backend health check / connection draining |
| `13-frontendconfig.yaml` | HTTP → HTTPS redirect |
| `14-managedcertificate.yaml` | Google-managed TLS certificate |
| `15-ingress.yaml` | GKE Ingress → Google Cloud Load Balancer |
| `16-worker-serviceaccounts.yaml` | Phase 8: 4 worker KSAs, one scoped GSA each (Workload Identity) |
| `17-worker-rbac.yaml` | Phase 8: 4 RoleBindings onto the existing `nimbusfs-app-role` |
| `18-deployment-outbox-publisher.yaml` | Phase 8: outbox → Pub/Sub publisher (1 replica) |
| `19-deployment-file-worker.yaml` | Phase 8: file-processing consumer + fan-out (2 replicas) |
| `20-deployment-thumbnail-worker.yaml` | Phase 8: thumbnail consumer (2 replicas, 1Gi memory limit) |
| `21-deployment-notification-worker.yaml` | Phase 8: notification consumer (1 replica) |

### Why 16–21 add no Service and no Ingress entry

There is no `Service` for any worker, and nothing was added to
`15-ingress.yaml`. Workers **pull** from Pub/Sub; nothing ever connects
*to* them. A Service exists to give a stable virtual IP to a set of pods
that receive traffic, and an Ingress exists to route external traffic to
a Service — neither question applies here, so inventing an answer would
just create an unused, internet-adjacent surface with no purpose.

Three things follow from that, and they are the parts worth remembering:

- **No readinessProbe on any worker.** "Ready" means "ready to be added
  to a Service's Endpoints." With no Service, readiness has no consumer;
  the only thing a readiness probe could achieve is marking a healthy
  worker not-Ready and stalling a rollout.
- **Liveness is an exec probe on `/tmp/healthy`**, not an HTTP GET —
  there is no HTTP server in these processes. The file is touched on a
  timer by a background task, deliberately independent of message
  arrival (an idle worker on an empty subscription is healthy), and the
  probe checks its *mtime*, not just its existence, so a wedged event
  loop is caught rather than papered over.
- **The default-deny NetworkPolicy (`11-networkpolicy.yaml`) already
  covers them.** Its egress allow-list includes Google APIs via Private
  Google Access, which is the path to Pub/Sub and GCS, plus Cloud SQL —
  everything a worker needs. No worker requires any *ingress* allowance
  at all, which is the strongest form of the point above. Note the same
  caveat as before: the Cloud SQL/Memorystore CIDRs in that file are
  still placeholders, and a wrong range now breaks event processing as
  well as the API.

Applying 16–17 before 18–21 matters for the usual reason (a Deployment
referencing a missing ServiceAccount stays Pending), and 05 must be
applied — or re-applied, since Phase 8 extended it — before any of them,
or the workers start with no topic names.

## One-time cluster setup

```bash
export PROJECT_ID=<your-project-id>
export REGION=us-central1
export CLUSTER_NAME=nimbusfs-cluster

gcloud config set project "$PROJECT_ID"

# VPC-native, private, regional cluster with Dataplane V2 (required for
# NetworkPolicy enforcement — see 11-networkpolicy.yaml) and Workload
# Identity enabled. See main README.md §22 "GKE Cluster Design" for why
# each flag is chosen, not just what it does.
gcloud container clusters create "$CLUSTER_NAME" \
  --region "$REGION" \
  --release-channel regular \
  --enable-dataplane-v2 \
  --enable-ip-alias \
  --workload-pool="${PROJECT_ID}.svc.id.goog" \
  --enable-private-nodes \
  --master-ipv4-cidr 172.16.0.0/28 \
  --enable-master-authorized-networks \
  --num-nodes 1 \
  --machine-type e2-standard-4 \
  --enable-autoscaling --min-nodes 1 --max-nodes 6

# Dedicated node pool for the application (referenced by
# 07-deployment.yaml's nodeAffinity) — kept separate from the cluster's
# default pool so app Pods aren't fighting cluster add-ons for capacity.
gcloud container node-pools create nimbusfs-app-pool \
  --cluster "$CLUSTER_NAME" --region "$REGION" \
  --machine-type e2-standard-4 \
  --enable-autoscaling --min-nodes 1 --max-nodes 6 \
  --node-labels=pool=nimbusfs-app-pool

gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION"
```

## Workload Identity setup

Binds `03-serviceaccount.yaml`'s Kubernetes ServiceAccount to a real GCP
IAM service account, so Pods get GCS/Cloud SQL credentials without ever
holding a key file:

```bash
gcloud iam service-accounts create nimbusfs-app \
  --display-name "NimbusFS application (GKE Workload Identity)"

# Scoped to the storage bucket only — same least-privilege principle as
# README.md §12's original GCS IAM setup, unchanged by this phase.
gsutil iam ch \
  serviceAccount:nimbusfs-app@${PROJECT_ID}.iam.gserviceaccount.com:roles/storage.objectAdmin \
  gs://nimbusfs-files-prod

# Cloud SQL client role, if connecting via the Cloud SQL Auth Proxy.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:nimbusfs-app@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

# The actual Workload Identity binding: lets the Kubernetes SA
# impersonate this GSA, scoped to exactly this one namespace+KSA name.
gcloud iam service-accounts add-iam-policy-binding \
  nimbusfs-app@${PROJECT_ID}.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:${PROJECT_ID}.svc.id.goog[nimbusfs/nimbusfs-ksa]"
```

Then edit `03-serviceaccount.yaml`'s `iam.gke.io/gcp-service-account`
annotation to match the real GSA email before applying.

## Secrets setup

**Never commit a real Secret manifest.** Preferred path — create it
imperatively, so the plaintext never touches disk as a file at all:

```bash
kubectl create namespace nimbusfs --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic nimbusfs-secrets \
  --namespace=nimbusfs \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_USER="nimbusfs" \
  --from-literal=POSTGRES_PASSWORD="<cloud-sql-password>" \
  --from-literal=REDIS_PASSWORD="<memorystore-auth-string-or-empty>"
```

Alternative: copy `06-secret.example.yaml` to `06-secret.yaml` (already
git-ignored — see `.gitignore`), fill in real values, `kubectl apply
-f k8s/06-secret.yaml`. See that file's header comment for the
future-Secret-Manager-CSI-driver upgrade path.

## Cloud SQL & Memorystore

Out of scope to fully script here (this phase deploys the application
tier, not the managed-database tier), but in short:

```bash
gcloud sql instances create nimbusfs-db \
  --database-version=POSTGRES_16 --tier=db-custom-2-8192 \
  --region="$REGION" --network=default --no-assign-ip

gcloud redis instances create nimbusfs-redis \
  --size=1 --region="$REGION" --network=default --tier=standard
```

Update `05-configmap.yaml`'s `POSTGRES_HOST`/`REDIS_HOST` with the real
private IPs these commands print, and `11-networkpolicy.yaml`'s
placeholder CIDRs to match your actual `--range` allocations, before
deploying.

## DNS & static IP

```bash
gcloud compute addresses create nimbusfs-ip --global
gcloud compute addresses describe nimbusfs-ip --global --format="value(address)"
# Point api.nimbusfs.example.com's A record at the printed IP, then
# update 14-managedcertificate.yaml and 15-ingress.yaml's placeholder
# domain to match.
```

TLS provisioning takes 15-60 minutes after DNS propagates. Check status:

```bash
kubectl describe managedcertificate nimbusfs-cert -n nimbusfs
# Look for: Status.CertificateStatus: Active
```

## Build & push the image

```bash
gcloud artifacts repositories create nimbusfs \
  --repository-format=docker --location="$REGION"

export TAG="v1.0.0" # or $(git rev-parse --short HEAD) for a per-commit tag
docker build \
  -f docker/Dockerfile \
  --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
  --build-arg BUILD_VERSION="$TAG" \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/nimbusfs/nimbusfs-api:${TAG}" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/nimbusfs/nimbusfs-api:${TAG}"
```

Update `07-deployment.yaml`'s `image:` field to match before deploying
— see main README.md §29 "CI/CD Preparation" for how this step is
intended to become automated in a future phase.

## Deploy

```bash
./scripts/k8s-deploy.sh
./scripts/k8s-smoke-test.sh          # read-only checks
./scripts/k8s-smoke-test.sh --full   # + self-healing + rollout/rollback demo
./scripts/k8s-scale-demo.sh          # HPA load demo
```

## kubectl command reference

```bash
# Pods / rollout status
kubectl get pods -n nimbusfs -o wide
kubectl get deployment nimbusfs-api -n nimbusfs
kubectl rollout status deployment/nimbusfs-api -n nimbusfs
kubectl describe pod <pod-name> -n nimbusfs      # events, probe failures

# Logs (structured JSON — Phase 4 — pipe through `jq` for readability)
kubectl logs -f deployment/nimbusfs-api -n nimbusfs | jq .
kubectl logs <pod-name> -n nimbusfs --previous    # logs from before the last restart

# Scaling
kubectl get hpa -n nimbusfs --watch
kubectl scale deployment/nimbusfs-api -n nimbusfs --replicas=5   # manual override; HPA will re-assert its own target on the next sync

# Deploy a new version (immutable tag — see 07-deployment.yaml's comment)
kubectl set image deployment/nimbusfs-api -n nimbusfs \
  nimbusfs-api=REGION-docker.pkg.dev/PROJECT_ID/nimbusfs/nimbusfs-api:v2.0.0
kubectl rollout status deployment/nimbusfs-api -n nimbusfs

# Rollback
kubectl rollout history deployment/nimbusfs-api -n nimbusfs
kubectl rollout undo deployment/nimbusfs-api -n nimbusfs                  # to the immediately previous revision
kubectl rollout undo deployment/nimbusfs-api -n nimbusfs --to-revision=3  # to a specific one

# Config/secret changes (no image change)
kubectl edit configmap nimbusfs-config -n nimbusfs
kubectl rollout restart deployment/nimbusfs-api -n nimbusfs   # Pods don't pick up ConfigMap/Secret changes until restarted
```

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Pod stuck `Pending` | Insufficient node capacity, or `nodeAffinity`'s preferred pool doesn't exist yet | `kubectl describe pod <pod>` → Events; `kubectl get nodes -l pool=nimbusfs-app-pool` |
| Pod `CrashLoopBackOff` | Startup probe failing — usually DB/Redis/GCS unreachable, `FAIL_FAST_ON_STARTUP=true` (app/main.py) makes the process exit | `kubectl logs <pod> -n nimbusfs --previous`; verify ConfigMap `POSTGRES_HOST`/`REDIS_HOST` and NetworkPolicy egress CIDRs match reality |
| Pod `Running` but never `Ready` | `/api/v1/ready` returning `503` — a real dependency is down, not an app bug | `kubectl exec <pod> -n nimbusfs -- curl -s localhost:8000/api/v1/ready \| jq .` |
| 502/503 from the Ingress, Pods look healthy | GCLB hasn't converged yet (NEG propagation is eventually-consistent, ~1-2 min after a Service/Deployment change), or BackendConfig health check path is wrong | `kubectl describe ingress nimbusfs-ingress -n nimbusfs`; Cloud Console → Load Balancing → Backend health |
| TLS not working | ManagedCertificate still `Provisioning`, or DNS doesn't point at the reserved static IP yet | `kubectl describe managedcertificate nimbusfs-cert -n nimbusfs` |
| HPA shows `<unknown>` for targets | Metrics Server not yet reporting (cold cluster) or Pods have no resource requests set (LimitRange should prevent this) | `kubectl describe hpa nimbusfs-api-hpa -n nimbusfs`; `kubectl top pods -n nimbusfs` |
| `kubectl create secret`/apply fails with a Pod Security Admission error | A manifest violates the namespace's `restricted` profile (00-namespace.yaml) — usually a missing `securityContext` field | Compare against 07-deployment.yaml's `securityContext` blocks exactly |
| Traffic still reaching a Pod after it fails readiness | Endpoint propagation lag (typically sub-second, GCLB NEG sync can lag longer) — same class of issue as the preStop sleep exists for | `kubectl get endpoints nimbusfs-api -n nimbusfs` — confirm the Pod's IP is actually gone from the list |

## Uninstall

```bash
kubectl delete -f k8s/15-ingress.yaml -f k8s/14-managedcertificate.yaml \
  -f k8s/13-frontendconfig.yaml -f k8s/12-backendconfig.yaml \
  -f k8s/11-networkpolicy.yaml -f k8s/10-pdb.yaml -f k8s/09-hpa.yaml \
  -f k8s/08-service.yaml -f k8s/07-deployment.yaml
kubectl delete secret nimbusfs-secrets -n nimbusfs
kubectl delete -f k8s/05-configmap.yaml -f k8s/04-rbac.yaml -f k8s/03-serviceaccount.yaml \
  -f k8s/02-limitrange.yaml -f k8s/01-resourcequota.yaml
kubectl delete namespace nimbusfs   # only once everything above is gone — deletes anything left over too
```
