# NimbusFS — CI/CD (Phase 12)

Companion to `docs/deployment.md` (environments/deployment mechanics),
`docs/release-process.md` (versioning/traceability),
`docs/rollback.md`, `docs/infrastructure.md` (Terraform/IaC), and
`docs/development-workflow.md` (day-to-day developer flow). Same
DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline as `docs/observability.md`
(Phase 11) — see that file's opening section for the definitions.

---

## 1. Repository inspection — what already existed (Phase 12 §1-4)

Before writing anything, inspected the repo for existing CI/CD, IaC,
and deployment tooling, per the brief's mandatory ordering. Found:

- **No CI/CD of any kind.** No `.github/`, no Cloud Build config, no
  Jenkinsfile. `README.md §"CI/CD preparation (not built)"` (written
  during Phase 5) explicitly documented the INTENDED shape as a future
  phase — this phase implements exactly that documented intent, not a
  different design invented from scratch.
- **No `pyproject.toml`/lint config**, despite `.ruff_cache`/
  `.mypy_cache` directories existing (leftover from ad-hoc local runs
  in earlier phases — e.g. Phase 10's `pip-audit` install/remove
  pattern) and despite ~40 `# noqa: BLE001`/`PLC0415`/`PERF203`/`B014`
  comments already scattered through `app/` implying a broader ruleset
  was always intended locally but never made explicit or enforced.
- **A complete, well-designed `docker/Dockerfile`** (Phase 5) already
  doing everything §13 of this phase asks for: multi-stage build, a
  fixed-UID non-root user matching `k8s/07-deployment.yaml`'s
  `securityContext` exactly, exec-form `CMD` for real SIGTERM handling,
  a `HEALTHCHECK` hitting `/live` (not `/health`), and — the key piece
  this phase's build pipeline depends on — `GIT_COMMIT`/`BUILD_VERSION`
  build args already wired straight into `Settings.BUILD_VERSION`/
  `GIT_COMMIT`, which every `/health` response and log line already
  exposes (Phase 4). **Not rewritten.**
- **`terraform/`** (Phase 9's extension) already provisions VPC/GKE/
  IAM/GCS/Pub/Sub/Artifact Registry, already has an `environment`
  variable with `dev`/`staging`/`prod` validation baked in, and already
  documents (in `versions.tf`'s backend comment) that local state is a
  known, called-out gap to fix "before a second person or a CI
  pipeline ever runs this" — i.e. this phase's remote-state work was
  already anticipated, not improvised.
- **`k8s/*.yaml`** (Phases 5, 8, 9, 11) already uses a safe rolling-
  update strategy with real readiness/liveness probes, PDBs, and
  `<PROJECT_ID>`/`REGION`/`:v1.0.0` placeholders explicitly documented
  as "replace before `kubectl apply`" — i.e. the manifests already
  assumed a substitution step would eventually automate that.
- **`scripts/k8s-deploy.sh`** only ever applied `00-15` — a real,
  pre-existing gap (it never applied the Phase 8/9 worker Deployments/
  CronJob or Phase 11's PodMonitoring). Documented, not silently fixed
  in place — see §9 below ("Known gap carried forward").

**Conclusion**: nothing here needed replacing. Phase 12 fills the
CI/CD gap the codebase already knew it had and already left the exact
seams for (build args, placeholders, an `environment` variable, a
"remote backend before CI" comment) — it does not introduce a
competing design.

## 2. Architecture decision

| Decision | Choice | Why |
|---|---|---|
| CI provider | **GitHub Actions** | The repo already lives on GitHub; no existing CI system to respect/replace; the brief explicitly says not to add Jenkins/GitLab/another CI provider. |
| GCP auth | **Workload Identity Federation (OIDC)** — zero long-lived keys | Phase 12 §15's explicit requirement; see §4 below. |
| Deployment mechanism | **Raw `kubectl apply` + `kubectl set image`/`rollout undo`**, no Helm/Kustomize/Argo CD | This is EXACTLY what `k8s/README.md` and `README.md`'s Phase-5-era "CI/CD preparation" section already documented as the intended shape — "CI would just be what triggers the same `kubectl` commands `k8s/README.md` documents doing by hand today." Introducing Helm/Kustomize/Argo CD now would replace a working, already-understood mechanism with a new one for no requirement this project actually has (one cluster, one image, ~7 Deployments+1 CronJob, numerically-ordered plain YAML that already applies cleanly). See "Why not Argo CD" below. |
| Branching | **Trunk-based**: `feature/*` -> PR -> `main` (protected, required checks) | Simplest workflow that satisfies "every PR gets checks, main is always deployable" — see `docs/development-workflow.md`. GitFlow's `develop`/`release/*` branches would exist to solve a problem this project doesn't have (multiple simultaneously-supported production versions); rejected explicitly rather than defaulted into. |
| Build-once-deploy-many | **Yes** — one image, built once per commit to `main`, tagged by git SHA, promoted unchanged from staging to production | The alternative (rebuilding per environment) risks environment drift from a dependency resolving differently at build time and is strictly more moving parts for zero benefit — see `docs/release-process.md`. |
| Environments | **development** (docker-compose, local) / **staging** / **production** — names match `terraform/variables.tf`'s pre-existing `environment` variable exactly | Preserves existing naming per Phase 12 §5's explicit instruction; see `docs/deployment.md` §"Environment strategy" for the project-isolation recommendation. |

### Why not Argo CD (Phase 12 §20, explicit ask to justify)

- **Operational cost**: a controller Deployment to run, upgrade, and
  secure inside the cluster, plus a second source of truth (Argo's
  Application CRDs) layered on top of the `k8s/*.yaml` that already
  exists — for one cluster and one application, this is pure overhead.
- **Architecture**: GitOps's actual value proposition — "the cluster's
  state continuously reconciles toward what's in Git, drift is
  detected and corrected automatically" — matters most at a scale
  (many clusters, many teams, frequent out-of-band `kubectl` changes)
  NimbusFS is not at. A push-based `kubectl apply` from a workflow that
  already runs on every merge to `main` gives the SAME "Git is the
  source of truth" property this project actually needs, without a
  second control plane.
- **Ownership/rollback**: with Argo CD, "what's actually running"
  requires understanding Argo's own sync state on top of Kubernetes'
  own Deployment/ReplicaSet state — one more layer to reason about
  during an incident, precisely when clarity matters most. Plain
  `kubectl rollout undo` (already documented in `k8s/README.md` since
  Phase 5, unchanged) stays the rollback mechanism — see
  `docs/rollback.md`.
- **When to revisit**: multiple clusters/environments needing
  continuous drift-correction, or a platform team operating many
  applications through one GitOps control plane — neither is true
  today, and this is recorded so revisiting it isn't a "why did nobody
  think of this" conversation later.

## 3. The pipeline

```
Developer
  |
  v
feature/* branch -> Pull Request into main
  |
  v
.github/workflows/ci.yml   (every PR + every push to main)
  |
  +-- lint (ruff check)                      BLOCKING
  +-- typecheck (mypy)                       BLOCKING
  +-- secret-scan (gitleaks)                 BLOCKING
  +-- dependency-audit (pip-audit)           BLOCKING
  +-- bandit                                 BLOCKING
  +-- test (pytest, 449 tests)               BLOCKING
  |     (all 6 BLOCKING jobs must pass ->)
  v
  build-and-push
  +-- docker build
  +-- Trivy container scan (CRITICAL/HIGH, ignore-unfixed) BLOCKING
  +-- [main only] push to Artifact Registry, tag = git SHA
  |
  v  (workflow_run: ci succeeded on main)
.github/workflows/deploy-staging.yml           NO approval needed
  +-- WIF auth as nimbusfs-ci-deploy
  +-- scripts/ci-deploy.sh  (kubectl apply + rollout wait)
  +-- scripts/k8s-smoke-test.sh                 verification
  +-- [on failure] scripts/ci-rollback.sh        auto-rollback
  |
  v  (human decides staging looks good)
.github/workflows/deploy-production.yml   workflow_dispatch, MANUAL
  +-- environment: production (required reviewers, GitHub-side)
  +-- WIF auth as nimbusfs-ci-deploy-prod (IAM-side: only trusts a
  |    token stamped environment=production, i.e. already approved)
  +-- scripts/ci-deploy.sh
  +-- scripts/k8s-smoke-test.sh
  +-- [on failure] scripts/ci-rollback.sh
```

`.github/workflows/terraform.yml` and `.github/workflows/rollback.yml`
run independently of the above — see `docs/infrastructure.md` and
`docs/rollback.md` respectively.

**Test pyramid, honestly** (Phase 12 §8): the 449-test suite is
overwhelmingly INTEGRATION-shaped, not a formal unit/integration/E2E
split — most tests drive the real FastAPI app through `httpx.AsyncClient`
against real route/service/repository code, with only the process
boundary (Postgres, Redis, GCS, Pub/Sub) faked (`tests/conftest.py`,
`tests/fakes/`). A smaller subset are true unit tests of pure logic in
isolation (`app/core/retry.py`, `circuit_breaker.py`, `path_utils.py`).
There is no E2E tier (a real cluster, real GCP) — that would require
infrastructure this project has never had access to in any session.
This works here specifically BECAUSE the fakes are real in-memory
implementations of the actual wire protocol (not mocks asserting call
counts), not despite skipping a "real" integration tier — the whole
suite still runs in ~2 minutes with no external services, which is
what keeps `ci.yml`'s `test` job fast enough to run on every single PR
rather than being gated behind a slower, separate "integration" label.

## 4. Authentication (Phase 12 §15-16)

No GCP service-account JSON key exists anywhere in this repository,
any GitHub Secret, or any workflow file. Every workflow that talks to
GCP uses `google-github-actions/auth@v2`, which exchanges a
short-lived GitHub-issued OIDC token for a short-lived (1 hour) GCP
access token via Workload Identity Federation — see
`terraform/cicd.tf`'s module docstring for the exact token-exchange
flow and `docs/infrastructure.md` for the WIF pool/provider resources
themselves.

**Four separate CI identities**, least-privilege, each scoped to
exactly what its job needs (Phase 12 §16's explicit requirement):

| Identity | Used by | Roles | Trust condition |
|---|---|---|---|
| `nimbusfs-ci-build` | `ci.yml` build-and-push | `artifactregistry.writer` (scoped to the `nimbusfs` repo only) | any run in this repo |
| `nimbusfs-ci-deploy` | `deploy-staging.yml` | `container.developer` (project-scoped — see `terraform/cicd.tf`'s residual-scope note) | OIDC token has `environment=staging` |
| `nimbusfs-ci-deploy-prod` | `deploy-production.yml` | `container.developer` | OIDC token has `environment=production` (i.e. already approved) |
| `nimbusfs-ci-terraform-plan` | `terraform.yml` plan (PRs) | `viewer` + `iam.securityReviewer` (read-only) | any run in this repo |
| `nimbusfs-ci-terraform-apply` | `terraform.yml` apply | per-service admin roles (see `terraform/cicd.tf`) | OIDC token has `environment=production-terraform` |

None of these is impersonable by a workflow run in a fork or a
different repository — `terraform/cicd.tf`'s WIF provider has a
project-wide `attribute_condition` requiring `assertion.repository`
to equal this exact repo, checked BEFORE any per-identity binding is
even considered.

**Runtime identities are unrelated and unchanged**: the 6 GSAs
`terraform/iam.tf` created in Phase 9 (`nimbusfs-app` and the 5
worker/reconciliation GSAs) authenticate the RUNNING application via
GKE's own Workload Identity (KSA -> GSA), a completely different
mechanism from GitHub's WIF above, and no CI identity can impersonate
any of them or vice versa.

## 5. Security gates (Phase 12 §10, §42)

| Gate | Tool | Scope | Blocking? |
|---|---|---|---|
| Secrets | `gitleaks` (via `gitleaks-action`) | full git history, every PR/push | **Yes** |
| Dependency CVEs | `pip-audit` | `requirements.txt` | **Yes** (9 pre-existing, individually-reviewed findings allow-listed — see §6) |
| Static analysis | `bandit` | `app/` | **Yes** (6 pre-existing, individually-reviewed findings suppressed via `# nosec` — see §6) |
| Container vulnerabilities | Trivy | the built image | **Yes** (CRITICAL/HIGH with a fix available) |
| Code quality | `ruff check` | `.` | **Yes** (curated rule set — see §6) |
| Formatting | `ruff format --check` | `.` | **No** — informational (see §6) |
| Type checking | `mypy` | `app/` | **Yes** (53 pre-existing findings — fixed, not suppressed; see §6) |
| Test coverage | `pytest-cov` | `app/` | **Yes** — 75% floor (measured baseline: 82%; see §6) |
| SBOM generation | Trivy (`cyclonedx` format) | the built image | No — visibility only, every PR+push (see §11) |
| Artifact signing | `cosign` (keyless/Sigstore) | the pushed image, main only | N/A — provenance, not a pass/fail gate (see §11) |

## 6. Lint/type/security scope — what's enforced, what's deferred, and why

Standing up CI against 11 phases of pre-existing code surfaced real,
pre-existing findings across every tool. Each was individually
reviewed (not blanket-suppressed) and falls into one of three buckets:

1. **Fixed** — safe, mechanical changes applied this phase: `ruff
   --fix` for import sorting, unused imports, `datetime.UTC`/pyupgrade
   syntax, plus ~15 hand-reviewed sites (a nested-if collapse, a
   ternary, `int(math.ceil(...))` redundant casts in a test fake, 6
   `# nosec B110` annotations on already-documented "log, never raise"
   `except Exception: pass` sites). **449/449 tests still pass**
   after every fix — re-run and verified, not assumed.

   **All 53 pre-existing `mypy` findings were also fixed this way**,
   once CI actually ran and surfaced them as blocking (they were
   initially shipped informational-only — see the git history on this
   file — then fixed on request rather than left deferred). None
   needed a behavior change or a blanket `# type: ignore`:
   - A shared `app/database/redis.py::eval_script` wrapper resolves
     `redis-py`'s stub declaring `Redis.eval`'s return as
     `Awaitable[str] | str` regardless of client flavor or script —
     one `cast`, three call sites (`distributed_lock.py`,
     `rate_limiter.py`), documented in the wrapper's own docstring.
   - `BaseRepository.get_by_id` (`app/repositories/base.py`) looks up
     by `class_mapper(self.model).primary_key[0]` instead of
     `self.model.id` — `Base` itself declares no `id` (each concrete
     model does independently), and this is also more correct: it no
     longer assumes every model's primary key column is literally
     named `id`.
   - `app/exceptions/handlers.py::_envelope` pins `APIResponse[None]`
     explicitly — `data=None` alone doesn't tell mypy what `T` is.
   - A handful of `assert`s narrow a real, already-true invariant mypy
     can't see across a function/variable boundary — e.g.
     `FileMetadataRepository.get_by_checksum` filters
     `object_name.is_not(None)` at the QUERY level
     (`app/services/file_upload_service.py`), and a chunk's
     `status == VERIFIED` is only ever set alongside a real
     `storage_reference` (`app/services/chunked_upload_service.py`).
     Each assert's comment names the exact guarantee it's asserting.
   - `CacheService | None` access after the `self._caching` PROPERTY
     check (`metadata_service.py`/`folder_service.py`, 4 sites): bound
     to a local variable and guarded directly (`if cache is None or
     not cache.enabled:`) instead — mypy narrows a local's type after
     an `is None` check, but doesn't see through an opaque property
     call to narrow `self._cache` itself.
   - `app/main.py::_register_handler` — a `TypeVar`-generic, one-`cast`
     wrapper around `add_exception_handler` for the genuine parameter-
     contravariance mismatch every FastAPI codebase with per-exception-
     type handlers hits (Starlette's stub wants
     `Callable[[Request, Exception], ...]`; each handler is correctly,
     usefully typed against its own specific exception subclass).
     Fixes all 20 `app/main.py` findings from one place instead of 20
     scattered `# type: ignore`s.
2. **Documented and suppressed at the exact site** — `pyproject.toml`'s
   `[tool.bandit]` skip list (B104/B608/B105/B108/B311/B101, each with
   a one-line "why this is safe in THIS codebase" reason) and 6
   `# nosec B110` comments; `dependency-audit.md`'s Phase 12 addendum
   for the 9 allow-listed CVE IDs.
3. **Documented and deferred, not enforced this phase**:
   - `BLE001`/`S110` (broad `except Exception`): ~35 sites across the
     codebase, most already a deliberate, documented "log, never
     raise" degradation contract (`CacheService`, `RateLimiter`,
     health checks). Several already-added `# noqa: BLE001`
     annotations turned out, on re-check, to be attached to lines that
     re-raise (not actually blind) — meaning a careful per-site audit
     is real, non-trivial work, properly scoped as its own pass, not
     something to rush through while standing up CI. Tracked here as
     an open gap, not silently ignored.
   - `ruff format --check`: 59 files would be reformatted (the
     codebase predates `ruff format` and was hand-wrapped before any
     formatter config existed). Reformatting 59 files as a side effect
     of adding CI is out of scope and risky; kept as a visible,
     non-blocking signal instead.
   - `UP042`/`UP046`/`UP047` (pyupgrade `StrEnum`/PEP 695 generics),
     `SIM105` (`contextlib.suppress`), `RUF022` (deliberately
     phase-grouped `__all__` in `app/models/__init__.py`): pure
     modernization/style, ignored in `pyproject.toml` with reasons.
4. **Measured and set as a real, non-trivial gate** — test coverage.
   `pytest --cov=app` was run for real against the actual 449-test
   suite: **82% overall**, MEASURED, not assumed. The CI/`make test`
   floor is set to **75%** (not 100% — Phase 12 §9 explicitly warns
   against an arbitrary maximal target), a genuine margin below the
   measured baseline rather than a number picked to always pass.
   Coverage is NOT evenly distributed across the specific areas §9
   calls out as deserving focus: `auth_service.py` 56%,
   `folder_service.py` 44%, `metadata_service.py` 46%,
   `chunked_upload_service.py` 57%, `version_service.py` 62%. Every
   one of these IS exercised by integration tests through the real
   HTTP API (`test_login.py`, `test_folders.py`, `test_metadata.py`,
   `test_chunked_upload.py`) — the uncovered lines are disproportionately
   error/retry/admin-only branches, not the primary success path — but
   that is a real, honest gap against the brief's own stated priority,
   not a claim that these modules are thoroughly tested. Left as a
   catalogued gap for a dedicated testing pass, exactly like the
   `BLE001`/`S110` gap above; writing new tests to close it is a
   larger, separate effort than standing up the coverage GATE itself.

This is the honest state of lint/type debt in this codebase as of
Phase 12 — a real baseline CI now protects going forward, not a claim
that everything already conformed to it. `mypy` is the one tool of
the three originally-deferred ("`ruff format`/`mypy`/`BLE001`-`S110`")
that moved from deferred to fully fixed and blocking.

## 7. Immutable image tags (Phase 12 §12)

Every image this pipeline builds is tagged with the git commit SHA
(`nimbusfs-api:<sha>`), never `:latest`. `:latest` is a **mutable
pointer** — `docker pull nimbusfs-api:latest` can silently return a
different image tomorrow than it did today, which breaks the single
most important property a deployment pipeline needs: "redeploying the
same tag redeploys the same bytes." A git-SHA tag is immutable by
construction (the same SHA is never reused for different content) and
doubles as the release-traceability key — see `docs/release-process.md`.

## 8. Deployment verification (Phase 12 §25)

`scripts/ci-deploy.sh`'s `kubectl rollout status` wait only proves
Kubernetes considers the rollout mechanically complete (desired
replicas ready). `scripts/k8s-smoke-test.sh` (Phase 5, reused
unchanged) runs AFTER that and independently checks: the namespace/
config objects, that a live Pod answers `/live`+`/ready` from inside
its own container (not just "the Pod object exists"), that the
Service has populated Endpoints, and that HPA/PDB/NetworkPolicy
objects are intact. A deploy is only reported successful in a workflow
summary after BOTH pass.

## 9. Known gap carried forward

`scripts/k8s-deploy.sh` (the human-run entrypoint, unchanged this
phase) only ever applied manifests `00`-`15` — it never applied the
Phase 8/9 worker Deployments/CronJob (`16`-`23`) or Phase 11's
PodMonitoring (`24`). `scripts/ci-deploy.sh` (this phase, CI-only)
applies the full `k8s/*.yaml` set and is now the single source of
truth for "what gets deployed." Fixing the human script's scope was
deliberately left alone — a smaller, separate, easily-reviewable
change from "add CI," not folded in as a silent side effect.

## 10. Supply-chain security: SBOM, signing, and the severity policy (Phase 12 §11, §14)

Added on top of the security gates in §5, both in `ci.yml`'s
`build-and-push` job:

**Vulnerability severity policy** (Phase 12 §11 — "define handling for
CRITICAL/HIGH/MEDIUM/LOW"):

| Severity | Policy |
|---|---|
| CRITICAL / HIGH (fix available) | **Blocks the build**, always, no override in CI itself. `ignore-unfixed: true` so an upstream-unpatched base-image finding doesn't permanently wedge every build — the same reasoning `docs/security/dependency-audit.md` already applies to `pip-audit`. |
| MEDIUM / LOW | **Not a blocking gate.** Still visible in the Trivy scan step's own log for every run. Triaged like any other accepted-risk finding — via the same "documented, reviewed, one-line reason" pattern `pyproject.toml`'s `[tool.bandit]`/`[tool.ruff.lint]` sections and `docs/security/dependency-audit.md`'s CVE allow-list already use — rather than either auto-failing on routinely-low-exploitability findings or silently ignoring them. No MEDIUM/LOW container findings have been triaged into that allow-list yet; none has been found to date (Trivy has never actually been run against a built image this session — see §11 below). |
| An exception is needed | Documented at the exact point of suppression (a `pyproject.toml` skip entry, a `--ignore-vuln`, a `# nosec`), never a blanket `--severity` downgrade or a disabled step. |

**SBOM (Software Bill of Materials)**: every build (PR and push alike)
generates a CycloneDX SBOM via Trivy and uploads it as a workflow-run
artifact (`actions/upload-artifact`, 90-day retention). This is
deliberately the SIMPLEST version of "have an SBOM" that's genuinely
useful — a PR reviewer or an incident responder can download the exact
dependency manifest for the exact commit in question — without
standing up a dedicated SBOM registry/database, which nothing about
NimbusFS's current scale or threat model justifies yet (Phase 12's own
"do not add complexity without justification" instruction, applied
here the same way Phase 11 applied it to Prometheus/Grafana).

**Artifact signing**: every image pushed to Artifact Registry (main
branch only) is signed, keyless, via `sigstore/cosign-installer` +
`cosign sign`. "Keyless" is the specific reason this was judged worth
adding rather than deferred: it costs no new secret, key, or IAM
identity to manage — the signature is bound to the GitHub Actions
OIDC token that already exists for this exact workflow run (the same
mechanism, applied to a different purpose, as the WIF authentication
already used for every GCP call in this pipeline), and is publicly
verifiable against Sigstore's Rekor transparency log. Signs the image
**digest**, not the mutable `:<sha>` tag pointer, so the signature is
bound to the exact bytes, independent of whatever the tag happens to
resolve to later.

**What this deliberately is NOT**: a full provenance/attestation
system (e.g. SLSA Build Level 3 in-toto attestations tying the
signature to the exact build inputs/steps), a private/paid container-
scanning SaaS, or a policy-enforcement admission controller in GKE
that actually REQUIRES a valid cosign signature before a Pod can run
(Kyverno/Gatekeeper policy — a real, reasonable next step once a
cluster exists to enforce it against; not added now because there is
no cluster to test it against, and an unenforced policy is a false
sense of security). Recorded as a deliberate scope boundary, not an
oversight.

## 11. Implementation status

| Capability | Status |
|---|---|
| PR lint/security/test gates | IMPLEMENTED, TESTED (every tool run for real against this codebase this session — see §6) |
| Docker build + Trivy scan | DESIGNED (workflow written, schema-reviewed) — Trivy itself NOT run this session (no Docker daemon available — see docs/deployment.md's honesty note) |
| WIF auth / CI IAM | DESIGNED — Terraform resources written, NOT applied (no real GCP project) |
| Staging/production deploy workflows | DESIGNED — NOT executed against a real cluster |
| Rollback | DESIGNED — `kubectl rollout undo` is Kubernetes' own documented rollback mechanism; NEITHER it nor this phase's wrapper scripts (`scripts/ci-rollback.sh`) have been executed against a real cluster in this or any prior session (no real cluster has ever been reachable — see `docs/deployment.md` §6) |
| Terraform plan/apply pipeline | DESIGNED — NOT validated with a real `terraform` binary this session (none installed) |
| Test coverage gate | IMPLEMENTED, MEASURED — `pytest --cov=app` actually run this session; 82% real baseline, 75% floor enforced |
| Type-check gate (mypy) | IMPLEMENTED, TESTED — all 53 pre-existing findings fixed for real and re-verified; `app` passes `mypy` cleanly, gate is blocking |
| SBOM generation | DESIGNED (workflow written, schema-reviewed) — NOT executed this session (same no-Docker-daemon constraint as the Trivy scan itself) |
| Artifact signing (cosign) | DESIGNED — NOT executed this session; no image was ever built or pushed to sign |
