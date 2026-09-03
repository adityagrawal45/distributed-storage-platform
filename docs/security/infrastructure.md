# Infrastructure Security — GCP IAM, Kubernetes, Secrets, Network, GCS

Everything in this file was **inspected in this session** against the
actual files (`k8s/*.yaml`, `terraform/*.tf`, `docker/Dockerfile`,
`app/services/storage_service.py`), not assumed from `CONTEXT.md`'s
narrative. Most of it was already correctly built in Phases 5/8/9 and
the Terraform pass earlier in this session — Phase 10's contribution
here is the inspection and this write-up, not new infrastructure code,
per the brief's own "if it's already correct, don't rewrite it" rule.

## GCP IAM — inspected, already least-privilege

`terraform/iam.tf` (and the K8s ServiceAccount annotations it feeds:
`k8s/03-serviceaccount.yaml`, `k8s/16-worker-serviceaccounts.yaml`)
define **6 separate Google service accounts**, one per component, not
one shared account:

| Component | GCS role | Pub/Sub role | Notes |
|---|---|---|---|
| `nimbusfs-app` (API) | `storage.objectAdmin`, scoped to the one application bucket | — | + `cloudsql.client` |
| `nimbusfs-outbox-publisher` | none | `publisher` on all 3 topics | never consumes |
| `nimbusfs-file-worker` | `objectViewer` | `subscriber` (file-events) + `publisher` (file + notification topics) | |
| `nimbusfs-thumbnail-worker` | `objectViewer` + `objectCreator` **scoped to the `thumbnails/` prefix only** via an IAM Condition | `subscriber` (thumbnail sub) | the one component decoding untrusted image bytes — narrowest write scope of all 6 |
| `nimbusfs-notification-worker` | none | `subscriber` (notification sub) | the only component that will ever talk to a third party (a future real email provider); deliberately has NO GCS role at all |
| `nimbusfs-reconciliation` | `objectViewer` | — | + `cloudsql.client`; read-only on both systems it inspects, matching `ReconciliationService` having no delete/update code path anywhere |

No `roles/owner` or `roles/editor` anywhere. No service-account JSON
key file is created or referenced — every binding is Workload Identity
(`google_service_account_iam_member` with `roles/iam.workloadIdentityUser`,
scoped to exactly one `namespace/KSA` pair per account).

**This table is not new** — it existed in `k8s/16-worker-serviceaccounts.yaml`'s
own header comment since Phase 8/9, and was reproduced in Terraform
form during this session's earlier IAM work. Phase 10 verified it
against the actual `.tf`/`.yaml` files rather than trusting the
comment.

## Kubernetes hardening — inspected, already hardened

`k8s/07-deployment.yaml` (API) and the Phase 8/9 worker Deployments:

```yaml
runAsNonRoot: true              # pod-level (line 98)
allowPrivilegeEscalation: false # container-level securityContext (line 316)
readOnlyRootFilesystem: true    # (line 317)
capabilities: { drop: [ALL] }   # (line 318 area)
```

No `privileged: true`, no `hostNetwork`, no `hostPID`, no `hostPath`
volume anywhere in `k8s/*.yaml` — grepped, confirmed absent.
`k8s/00-namespace.yaml` applies the Pod Security Standards `restricted`
profile at the namespace level (the `securityContext` blocks above
exist specifically to satisfy it — a manifest violating it is rejected
at admission, not merely discouraged). `k8s/11-networkpolicy.yaml` is a
default-deny policy. Each component has its own dedicated
ServiceAccount + least-privilege RBAC (`k8s/04-rbac.yaml`,
`k8s/17-worker-rbac.yaml`) — NimbusFS doesn't call the Kubernetes API
from application code at all today, so even that RBAC is close to
inert defense-in-depth, not a functional requirement.

**Not verified against a live cluster** — same honest caveat every
prior phase's K8s work carries (`CONTEXT.md`'s "Phase 5 Verification
Caveat"): validated by manifest/YAML inspection only, no real GKE
cluster exists for this project as of this session.

## Container security — inspected, already correct

`docker/Dockerfile`: multi-stage (`builder` + `runtime`), `python:3.12-slim`
base (not a bloated full image), non-root user, no secret ever baked
into a layer (checked — `docker/Dockerfile` contains no `ENV
*_SECRET*`/`*_PASSWORD*`/`*_KEY*` with a literal value, only
`ARG`/`ENV` for non-secret build metadata like `GIT_COMMIT`).
`.dockerignore` exists and excludes `.env`, `__pycache__`, `.git`.
Dependencies are pinned to exact versions in `requirements.txt` (see
`dependency-audit.md` for what was found when those pins were actually
checked against known CVEs).

## Secrets management — inspected, correct pattern documented, not fully appliable today

`k8s/06-secret.example.yaml` + `k8s/README.md`'s "Secrets setup"
section document the correct pattern: never commit a real Secret
manifest (`k8s/06-secret.yaml` is gitignored), create it imperatively
via `kubectl create secret` so plaintext never touches disk as a
tracked file. This is a real, already-correct practice — verified
`.gitignore` actually excludes it (confirmed in an earlier session on
this project).

**Gap, honestly recorded**: this is a Kubernetes Secret, not Google
Secret Manager. The Phase 10 brief's preferred chain (`Secret Manager
→ Workload Identity → Application`) is NOT what's implemented — K8s
Secrets are base64-encoded, not encrypted-at-rest-with-a-KMS-key by
default (GKE does encrypt Secrets at rest at the etcd layer if
Application-Layer Secrets Encryption is enabled on the cluster, which
Terraform does not currently configure). Migrating to Secret Manager +
the CSI driver is a real, valuable next step, correctly identified by
`06-secret.example.yaml`'s own header comment as a "future...upgrade
path" — not built this phase, since it is genuine new infrastructure
work, not a hardening of something broken.

## Network security — inspected, already correct

`terraform/vpc.tf` (this session's earlier work): a dedicated VPC (not
`default`), private GKE nodes (no public IPs), Cloud NAT for egress,
Private Google Access for reaching Google APIs without traversing NAT.
Postgres and Redis are never given public IPs anywhere in the
Terraform/K8s config — `k8s/README.md`'s Cloud SQL/Memorystore setup
commands both pass `--no-assign-ip`/private networking. TLS: GKE
Ingress + ManagedCertificate (`k8s/14-managedcertificate.yaml`) for
inbound; `Strict-Transport-Security` header set by
`SecurityHeadersMiddleware` for the browser side.

## Security headers & CORS — inspected, already correct

`app/middleware/security_headers.py` sets
`X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`/
`Permissions-Policy`/`Strict-Transport-Security` on every response — no
CSP, deliberately (a JSON API, not server-rendered HTML, per the
file's own docstring).

`CORS_ALLOWED_ORIGINS` defaults to `http://localhost:3000` (NOT `*`) —
already safe-ish out of the box. `ALLOWED_HOSTS` (TrustedHostMiddleware)
defaults to `*` — this IS a real "must override in production" item,
though not a code defect: it is documented as a dev convenience in
`settings.py`, and a production deployment is expected to set
`ALLOWED_HOSTS` to the real domain via the K8s ConfigMap/environment.
**No enforcement exists that a production deployment actually does
this** — same class of gap as the JWT-secret-default note in
`authentication.md`. Recorded as a remaining risk, not fixed this
phase (a startup assertion would be the cheap fix, same shape as the
JWT one).

## GCS & signed URL security — inspected, already correct

- Bucket is private, `uniform_bucket_level_access` +
  `public_access_prevention = "enforced"` (`terraform/storage.tf`).
- No client input ever selects an arbitrary bucket/object — `object_name`
  is always server-generated (`StorageService.generate_object_name`,
  a UUID-based key namespaced by owner) or, for downloads, resolved
  from the already-ownership-checked `FileMetadata` row. A client
  cannot request an object outside its authorization scope because it
  never supplies the object name at all.
- Signed URLs: V4, time-boxed (`SIGNED_URL_EXPIRATION_MINUTES`, default
  15, capped 1–10080 minutes by the route's own `Query(ge=1, le=10080)`),
  method-restricted (defaults `GET`), issued only AFTER
  `get_downloadable_file`'s ownership check
  (`app/services/file_upload_service.py::get_signed_url`). Never
  logged (`storage_service.py` logs `object_name`, never the URL
  itself). Never persisted anywhere.
- Phase 10 verified this chain end-to-end in
  `tests/test_security_phase10.py::test_a_user_cannot_get_a_signed_url_for_another_users_file`
  and `::test_signed_url_writes_a_file_download_audit_row_without_logging_the_url`.
