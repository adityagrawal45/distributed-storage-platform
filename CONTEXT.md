# NimbusFS — Project Context

Purpose of this file: give a fresh AI session (or human) full context on this project in one read, without needing to re-explore the codebase from scratch. Written 2026-08-04; updated 2026-08-05 after completing Phase 4; updated 2026-08-08 after completing Phase 5; updated 2026-08-10 after completing Phase 6.

## Current Status: Phases 1–6 complete, repo healthy

The repo previously had **committed, unresolved Git merge-conflict markers** in 8 files (from a bad merge, `086377c "Merged existing repository"`) plus a parallel orphaned legacy implementation tree. **All of that has been resolved** — see "History: What Was Fixed" below for the record. As of now:

- `app.main` imports cleanly, the app starts, all routes are live.
- Full test suite: **145/145 passing** (57 Phase 1/2 + 19 Phase 3 + 28 Phase 4 + 41 Phase 6), against in-memory SQLite (`aiosqlite`) — no external services needed to run `pytest`. `/health`/`/ready` deliberately check *real* DB/Redis connectivity (see `app/database/session.py`/`redis.py`), so those two routes' test assertions are shape-only, not "must be healthy" — see `tests/conftest.py` for how the suite still stays fast without real infra. Phase 5 added no application code (manifests/Docker/scripts only); Phase 6 added a full new feature (chunked/resumable uploads) and all 41 new tests pass alongside the pre-existing 104 with zero regressions.
- Phase 5's `k8s/` manifest set + `scripts/k8s-*.sh` runbook scripts were verified syntactically only (`python -c "import yaml..."`, `bash -n`) — **not** applied against a real GKE cluster in this session (none available); see "Phase 5 Verification Caveat" below. This caveat is unchanged by Phase 6 (Phase 6 touched no `k8s/` files).
- No known unresolved conflicts, no orphaned legacy code, no stray env files.

## What NimbusFS Is

A **cloud-native distributed file storage platform** (Google-Drive-style) built with Python, FastAPI, PostgreSQL, and Google Cloud Storage. Built in phases (~15-phase roadmap). Currently implemented:

- **Phase 1**: user registration/auth (JWT access+refresh, role-based access)
- **Phase 2**: folder hierarchy, file metadata, soft-delete/trash, versioning, search & pagination
- **Phase 3**: real file upload/download via Google Cloud Storage, signed URLs, streaming downloads with Range support, SHA-256 content-based duplicate detection, upload/metadata rollback consistency
- **Phase 4**: distributed backend architecture — stateless multi-replica design, correlation/trace/server-ID propagation + structured logging, `/health`+`/ready`+`/live` endpoints, fail-fast startup + graceful shutdown lifecycle, Redis-backed distributed locks, Redis-backed `Idempotency-Key` support on `POST /files/upload`, DB/Redis/Storage retry-with-backoff, a circuit breaker primitive, trusted-proxy/forwarded-header handling, a rate-limit middleware placeholder.
- **Phase 5**: Kubernetes deployment on GKE — full manifest set in `k8s/` (Namespace, ResourceQuota/LimitRange, ServiceAccount+RBAC via Workload Identity, ConfigMap/Secret, Deployment with startup/readiness/liveness probes + rolling strategy + pod anti-affinity/node affinity, ClusterIP Service with container-native load balancing, HPA 3→10 on CPU+memory, PodDisruptionBudget, default-deny NetworkPolicy, GKE Ingress + BackendConfig + FrontendConfig + ManagedCertificate), a hardened multi-stage non-root Dockerfile (now the single canonical one for dev + prod), and `scripts/k8s-deploy.sh`/`k8s-smoke-test.sh`/`k8s-scale-demo.sh`.
- **Phase 6**: large-file chunked/resumable uploads — a second upload path (`/api/v1/uploads/*`, distinct from Phase 3's `/files/upload`) supporting arbitrarily large files via independent-temp-object-per-chunk + GCS-native Compose (NOT a single GCS resumable session — that's sequential-only and can't support genuine parallel chunk upload, see README §13 for the full research-backed rationale), explicit `UploadSession`/`UploadChunk` state machine, resumability (lazy expiration, live-computed progress), per-chunk + final SHA-256 checksums, Redis-lock-guarded concurrency control with a real DB unique constraint as the ultimate duplicate-chunk guarantee, `Idempotency-Key` reuse (same `IdempotencyService` as Phase 4) on initiate/complete, and k6/Locust load-test scripts. No Pub/Sub, background workers (including no automatic reconciliation of a session stuck mid-`COMPLETING` after a process crash — see README §13's "Advanced" interview question), full Redis caching, disaster recovery, multi-region, CI/CD, full observability stack, or content-dedup extension to the chunked path — those are future-phase/explicitly-out-of-scope.

**Not yet built** (future phases, per README §22): sharing/permissions between users, virus scanning (placeholder only), thumbnails, full-text content search, Pub/Sub background workers, real rate limiting, Redis *metadata* caching (Phase 4 only built the plumbing), content-dedup extension to chunked uploads (Phase 6), CI/CD automation (Phase 5 only documented the intended shape), Terraform, observability/OpenTelemetry tracing (Phase 5 only prepared Prometheus annotations), multi-region deployment, disaster recovery.

## Tech Stack

| Concern | Choice |
|---|---|
| Framework | FastAPI 0.115.x + Uvicorn (ASGI) |
| Validation/config | Pydantic v2 + pydantic-settings |
| Database | PostgreSQL 16 (`postgres:16-alpine` in docker-compose) |
| DB driver | `asyncpg` (app), `psycopg2-binary` (Alembic, sync) |
| ORM | SQLAlchemy 2.0, async, `Mapped`/`mapped_column` style |
| Migrations | Alembic 1.14 (`alembic/versions/`) |
| Cache | Redis 7 (currently only used for health checks) |
| Auth | JWT (`python-jose[cryptography]`) + `passlib[bcrypt]`/`bcrypt`; OAuth2 Password flow with access+refresh token rotation |
| Logging | `structlog` (structured, JSON-toggleable via `LOG_JSON`) |
| Cloud Storage | `google-cloud-storage` SDK, private bucket, V4 signed URLs, MIME sniffing via `filetype` |
| Testing | `pytest` + `pytest-asyncio` (`asyncio_mode = auto`), `httpx.AsyncClient`, in-memory SQLite (`aiosqlite`), hand-written `FakeGCSClient` (`tests/fakes/fake_gcs.py`) — no external services or real GCS needed |
| Containers | `docker/Dockerfile` (python:3.12-slim, multi-stage, non-root — Phase 5: now the single canonical Dockerfile for both dev and prod) + `docker-compose.yml` (postgres, redis, app) |
| Orchestration | Kubernetes on GKE (Phase 5) — see `k8s/` (16 manifests) + `k8s/README.md`; not applied to a live cluster in this repo/session, manifests only |

## Directory Map (live code)

```
app/
  main.py                    create_application() factory, lifespan (Phase 4: fail-fast startup dependency
                              verification + graceful shutdown), middleware, exception handlers, mounts api_router
  api/v1/
    router.py                 wires all sub-routers together, mounted at settings.API_V1_PREFIX (/api/v1)
    auth/routes.py             /auth/* endpoints
    users/routes.py            /users/* endpoints
    folders/routes.py          /folders/* endpoints
    metadata/routes.py         /metadata/* endpoints (Phase 2, metadata-only, no GCS awareness)
    files/routes.py            /files/* endpoints (Phase 3: upload/download/signed-url/replace/permanent-delete;
                                Phase 4: Idempotency-Key support on upload)
    trash/routes.py            /trash endpoint
    health/routes.py           /health, /ready, /live (Phase 4 — was /health only)
    uploads/routes.py          Phase 6: /uploads/* chunked-upload endpoints (thin — see chunked_upload_service.py)
  core/
    config/settings.py         Settings, get_settings() — includes GCS_*, MAX_UPLOAD_SIZE_MB, ALLOWED_MIME_TYPES,
                                BLOCKED_EXTENSIONS, Phase 4: INSTANCE_ID/HOSTNAME/BUILD_VERSION/GIT_COMMIT,
                                TRUSTED_PROXIES, IDEMPOTENCY_*, LOCK_*, RETRY_*, FAIL_FAST_ON_STARTUP, and
                                Phase 6: CHUNK_MIN/MAX/DEFAULT_SIZE_BYTES, MAX_CHUNKS_PER_UPLOAD,
                                MAX_CHUNKED_UPLOAD_SIZE_GB, UPLOAD_SESSION_EXPIRATION_MINUTES
    security/password.py       hashing (bcrypt)
    security/tokens.py         JWT encode/decode, TokenType, decode_token()
    enums.py                   UserRole, FileStatus, etc.; Phase 6: UploadSessionStatus, ChunkStatus
    upload_state_machine.py    Phase 6: UploadStateMachine — centralized valid-transition graph for UploadSessionStatus
    server_info.py             Phase 4: get_server_identity() — instance_id/hostname/pid/version/build singleton
    retry.py                   Phase 4: retry_async() — exponential backoff + full jitter
    circuit_breaker.py         Phase 4: CircuitBreaker — closed/open/half-open primitive, in-process per instance;
                                Phase 6: first real caller (ChunkedUploadService wraps GCS Compose calls with it)
    distributed_lock.py        Phase 4: DistributedLock/DistributedLockFactory — Redis SET NX PX + Lua-checked release
  database/
    session.py                  async engine/session, declarative Base; Phase 4: pool tuning, retry-wrapped
                                 check_database_connection(), run_with_deadlock_retry(), close_db_engine(),
                                 documented (not-yet-wired) read/write-separation + optimistic-locking design
    redis.py                    redis pool + check_redis_connection() (Phase 4: retry-wrapped), close_redis_pool()
    gcs.py                      GCS client factory (ADC in prod, key file path in dev) — Phase 3;
                                 Phase 4: check_storage_connection() bucket-verification health check
  dependencies/
    auth.py                     get_current_user, CurrentUser, require_role(); Phase 4: stashes request.state.user_id
    providers.py                DI wiring for repositories/services, incl. StorageServiceDep/FileUploadServiceDep/
                                 GCSClientDep, Phase 4: DistributedLockFactoryDep/IdempotencyServiceDep, and
                                 Phase 6: UploadSessionRepositoryDep/UploadChunkRepositoryDep/ChunkedUploadServiceDep
  models/                       user.py, refresh_token.py, folder.py, file_metadata.py (+Phase 3 storage columns),
                                 file_version.py, mixins.py, Phase 6: upload_session.py (AuditMixin), upload_chunk.py
                                 (own Python-side updated_at onupdate — see its docstring for the History #5 bug it avoids)
  repositories/                 base.py (+Phase 6: flush() helper) + one repo per entity; file_metadata_repository.py
                                 has get_by_checksum/object_name_in_use for dedup; Phase 6: upload_session_repository.py
                                 (get_owned), upload_chunk_repository.py (create_or_get_existing — SAVEPOINT-guarded
                                 insert, sum_verified_bytes, list_verified_ordered, delete_all_for_upload)
  services/                     auth_service.py, user_service.py, folder_service.py, metadata_service.py,
                                 search_service.py, trash_service.py, version_service.py,
                                 storage_service.py (Phase 3, GCS wrapper — ONLY module importing google.cloud.storage;
                                 Phase 6: +compose_objects [multi-stage GCS Compose], delete_many, compute_object_checksum),
                                 file_validation_service.py (Phase 3), file_upload_service.py (Phase 3 orchestrator),
                                 idempotency_service.py (Phase 4: Redis-backed Idempotency-Key contract, reused
                                 unchanged by Phase 6), chunked_upload_service.py (Phase 6: the whole chunked-upload
                                 orchestration — see its long module docstring for the full design writeup)
  schemas/                      auth.py, user.py, folder.py, file_metadata.py (+FileUploadResponse/SignedUrlResponse),
                                 health.py (Phase 4: ServerInfo/ReadinessResponse/LivenessResponse, HealthCheckResponse
                                 restructured — version/environment now nested under `server`),
                                 pagination.py, response.py (APIResponse[T] envelope), search.py, sorting.py,
                                 upload.py (Phase 6: UploadInitiateRequest/Response, UploadProgressRead, ChunkRead,
                                 ChunkUploadResponse, UploadCompleteResponse, UploadCancelResponse)
  exceptions/                   custom_exceptions.py (+Storage* exceptions; Phase 4: LockAcquisitionException,
                                 CircuitBreakerOpenException, ServiceUnavailableException,
                                 IdempotencyKeyReplayedException, IdempotencyKeyInProgressException; Phase 6: 9 new
                                 exceptions, ALL subclassing already-registered bases — zero new handler functions,
                                 zero main.py changes, see Phase 6 Design Decisions below),
                                 handlers.py (matching handlers for Phase 1-4 exceptions only — Phase 6 needed none)
  logging/logger.py             structlog config, get_logger()
  middleware/                   request_context.py (Phase 4: adds correlation_id/trace_id/server_id, more response
                                 headers), security_headers.py, proxy_headers.py (Phase 4: TrustedProxyMiddleware),
                                 rate_limit.py (Phase 4: RateLimitPlaceholderMiddleware — explicit no-op)
  utils/                        path_utils.py (materialized-path helpers), response.py
alembic/versions/               0001_initial, 0002_metadata, 0003_storage (adds GCS columns to file_metadata),
                                 Phase 6: 0004_chunked_uploads_add_upload_sessions_and_chunks (creates upload_sessions,
                                 upload_chunks + their 2 enum types) — no migration in Phase 4/5 (no model changes)
tests/
  conftest.py                  client/db fixtures + fake_gcs_client/fake_redis_client fixtures (override
                                get_gcs_client/get_redis for every test); Phase 4: pins RETRY_* env vars low
                                before app import so /health-touching tests stay fast; Phase 6: also pins
                                CHUNK_MIN_SIZE_BYTES=1024 (same pattern) so chunk tests stay fast
  fakes/fake_gcs.py             FakeGCSClient/FakeBucket/FakeBlob — in-memory GCS stand-in, no real network calls;
                                Phase 6: +FakeBlob.compose() (real byte-concatenation, not a call-count mock)
  fakes/fake_redis.py           Phase 4: FakeRedisClient — in-memory stand-in for the redis.asyncio surface
                                 NimbusFS actually uses (set/get/delete/eval/ping), no real Redis needed
  test_health/registration/login/protected_routes/folders/metadata/search.py   Phase 1/2 tests
  test_file_storage.py          Phase 3 tests (upload/download/range/signed-url/replace/permanent-delete/dedup/rollback/failure)
  test_distributed.py           Phase 4 tests (idempotency, distributed locks, retry, circuit breaker, correlation
                                 IDs, graceful degradation, concurrency) — see README §21 for the full list
  test_chunked_upload.py        Phase 6: 41 tests (initiate/chunk-upload/resume/expiration/cancellation/completion/
                                 idempotency/concurrency/ownership/state-machine/DB-GCS-Redis-failure/size-validation)
                                 — see README §13 "Testing" for the full list
k8s/                             Phase 5: Kubernetes manifests, numerically prefixed for apply order (00-namespace
                                  through 15-ingress) — see k8s/README.md for the full table + deployment runbook.
                                  Not pytest-testable (no cluster in this environment); validated via YAML parse +
                                  `kubectl apply --dry-run=client` guidance only, see "Phase 5 Verification Caveat" below.
                                  Untouched by Phase 6.
docker/Dockerfile                Phase 5: single canonical multi-stage, non-root Dockerfile (was previously
                                  duplicated with a root-level single-stage `dockerfile` — that duplicate was
                                  deleted this phase; docker-compose.yml now builds from docker/Dockerfile explicitly)
scripts/
  run_dev.sh / migrate.sh        pre-existing local dev helpers
  k8s-deploy.sh                  Phase 5: applies all k8s/ manifests in order, waits for rollout
  k8s-smoke-test.sh              Phase 5: read-only cluster checks by default; `--full` adds a self-healing
                                  (delete-a-Pod) demo and a rolling-update/rollback demo
  k8s-scale-demo.sh              Phase 5: drives synthetic load against the in-cluster Service to observe the HPA
                                  scale 3→10 and back down
  load-test/k6-chunked-upload.js Phase 6: k6 load test — 100 concurrent VUs, parallel chunk upload via http.batch,
                                  configurable chunk-corruption/resume-simulation rates
  load-test/locustfile.py        Phase 6: Locust equivalent (gevent-greenlet parallelism), for teams on Python tooling
  load-test/README.md            Phase 6: how to run both, what metrics to watch, explicit "what NOT to conclude" caveats
```

## API Surface (all under `/api/v1`)

Every response uses the standard envelope, `app/schemas/response.py::APIResponse[T]`: `{success, message, data, errors, timestamp, request_id}`.

**Health** (Phase 4 — was `/health` only): `GET /health` (deep check), `GET /ready` (LB/readiness probe, `503` if not ready), `GET /live` (liveness, no dependency checks)

**Auth** (`/auth`): `POST /register`, `POST /login` (OAuth2 form), `POST /refresh`, `POST /logout`

**Users** (`/users`, Bearer): `GET /me`, `GET /{user_id}` (admin only)

**Folders** (`/folders`, Bearer): full CRUD + tree/breadcrumb/trash/restore/permanent-delete (see README §5)

**File Metadata** (`/metadata`, Bearer, Phase 2 — metadata rows only, no bytes): CRUD, search, rename, move, trash/restore/permanent-delete, versions

**Files** (`/files`, Bearer, Phase 3 — actual bytes in GCS):
- `POST /files/upload` — multipart upload; creates metadata + bytes atomically (rollback on failure); Phase 4: optional `Idempotency-Key` header makes retries safe (replays cached response instead of re-uploading)
- `GET /files/{id}/download` — streaming, supports `Range` header (206 partial content)
- `GET /files/{id}/signed-url?expires_in_minutes=` — time-boxed V4 signed URL
- `PUT /files/{id}/replace` — new version, new object, old object cleaned up after swap
- `DELETE /files/{id}/permanent` — deletes GCS object (if unshared) + DB row; requires prior soft-delete via `/metadata/{id}`

**Trash** (`/trash`, Bearer): `GET /trash` — combined `{folders, files}`

**Uploads** (`/uploads`, Bearer, Phase 6 — chunked/resumable large-file uploads, distinct from `/files/upload`):
- `POST /uploads` — initiate a session (`Idempotency-Key` supported)
- `GET /uploads/{id}` — status/progress: `uploaded_chunks`, `missing_chunks`, `progress_percentage`
- `GET /uploads/{id}/chunks` — list chunk records
- `PUT /uploads/{id}/chunks/{n}` — upload one chunk (raw bytes body; optional `X-Chunk-Checksum` header)
- `POST /uploads/{id}/complete` — finalize (Compose + checksum verify + create FileMetadata; `Idempotency-Key` supported; safe against duplicate calls regardless)
- `POST /uploads/{id}/cancel` — abort (idempotent)
- `DELETE /uploads/{id}` — cancel-if-active + hard-delete the session record

## Data Model highlights

- **FileMetadata** (`file_metadata`) — Phase 2 columns (id, owner_id, folder_id, original_filename, stored_filename [unique per-row reservation], extension, mime_type, size, checksum, version, status) **plus Phase 3 storage columns**: `storage_provider`, `bucket_name`, `object_name` (indexed, **deliberately NOT unique** — content-dedup lets multiple rows share one object), `public_url` (always NULL — bucket is private), `storage_class`, `etag`, `upload_status` (pending/completed/failed), `uploaded_at`.
- `AuditMixin.updated_at` uses a **Python-side** `onupdate=lambda: datetime.now(timezone.utc)`, not a server-side `func.now()` — this was a real bug fix (see History below); don't revert it to a server-side onupdate, it will reintroduce an async `MissingGreenlet` crash on any mutate-then-serialize request. `UploadChunk.updated_at` (Phase 6) uses the identical Python-side pattern for the same reason.
- **UploadSession** (`upload_sessions`, Phase 6) — `AuditMixin` only (no `SoftDeleteMixin` — its own `status` enum already captures lifecycle more precisely than a soft-delete flag would). Columns: id, owner_id, folder_id (nullable), file_id (nullable, set only on COMPLETED), filename, mime_type, total_size, chunk_size, total_chunks, uploaded_bytes (written exactly once, atomically, at completion — never incremented per chunk), status (`upload_session_status` enum), storage_bucket, storage_object (final object key, reserved at initiate), gcs_upload_id (reserved/unused by the default Compose-based path), checksum_algorithm, expected_checksum, actual_checksum, idempotency_key, expires_at, completed_at, cancelled_at.
- **UploadChunk** (`upload_chunks`, Phase 6) — no mixins (immutable, short-lived, high-write record). Columns: id, upload_id (FK, CASCADE), chunk_number, size, checksum, status (`upload_chunk_status` enum: pending/uploaded/verified/failed), storage_reference (the chunk's own temp GCS object key), uploaded_at, created_at, updated_at. `UniqueConstraint(upload_id, chunk_number)` is the real duplicate-chunk guarantee — see Phase 6 Design Decisions.

## Phase 3 Design Decisions (see README §10 for full detail)

- Object naming: `{tenant}/{owner_id}/{year}/{month}/{uuid4}.{ext}` — never the user's filename.
- Duplicate detection: SHA-256 based; identical content reuses the existing `object_name` instead of re-uploading. This is *why* `object_name` has no unique constraint.
- Upload rollback: if metadata persistence fails after a real (non-deduped) GCS upload succeeded, the orphaned object is deleted; if that delete also fails, raises `RollbackFailedException` rather than swallowing it.
- Replace: uploads to a brand-new object, swaps metadata, *then* deletes the old object (only if unreferenced) — never overwrites in place.
- Soft delete (`/metadata/{id}`) never touches GCS bytes (recoverable via restore); permanent delete (`/files/{id}/permanent`) is the only path that removes bytes, and only if no other row still shares the object.
- Buckets are always private; signed URLs are the only sanctioned direct-access path.

## Phase 4 Design Decisions (see README §11 for full detail)

- Stateless-by-construction: no sessions, no server-local cache, no local-disk temp files. Everything a request depends on lives in Postgres, Redis, GCS, or the JWT itself.
- Three distinct probe endpoints, not one: `/health` (deep, for humans/dashboards), `/ready` (deep, `503` when unhealthy — for the load balancer), `/live` (instant, zero dependency checks — a slow DB must never trigger a liveness-probe restart).
- Three distinct IDs per request, not one: `request_id` (per-hop, always server-generated), `correlation_id` (per logical client operation, honored from `X-Correlation-ID` if supplied), `trace_id` (reserved for future OpenTelemetry, defaults to `correlation_id` today).
- `Idempotency-Key` support is scoped to `POST /files/upload` only this phase, not applied blanket across every mutating endpoint — see `app/services/idempotency_service.py`'s docstring for the fingerprinting trade-off (hashes filename/folder/content-type, not full file bytes, to avoid buffering the whole upload before starting it).
- Read/write DB separation and row-level optimistic locking are **designed and documented, not wired up** — see `app/database/session.py`'s module docstring for exactly why (no read-replica exists yet to route to; `FileMetadata.version` already means "content version," so a row-level lock counter needs its own new column in a future migration, not a reuse of that field).
- Circuit breaker is in-process per replica, deliberately not Redis-shared (a breaker's job is protecting *this process's* outbound calls; coordinating that via Redis would add a round-trip to every call it's meant to fast-fail).
- Rate limiting is an explicit no-op placeholder (`app/middleware/rate_limit.py`) — infrastructure/seam only, no real limiting yet, per this phase's scope.

## Phase 5 Design Decisions (see README §12 for full detail)

- GKE-native Ingress (not nginx-ingress or another 3rd-party controller) — fewer moving parts for a single-cluster, single-cloud deployment; native integration with ManagedCertificate/BackendConfig/NEGs.
- Container-native load balancing (NEGs, `08-service.yaml`'s `cloud.google.com/neg` annotation) — GCLB routes to Pod IPs directly, skipping a kube-proxy hop and giving GCLB accurate per-Pod health via BackendConfig.
- Google-managed TLS certs (ManagedCertificate CRD), not cert-manager + Let's Encrypt — zero extra components to operate for this phase's scope.
- Soft (`preferred...`) pod anti-affinity/node affinity, not hard (`required...`) — a hard requirement with only 3 replicas across 3 zones could leave a Pod permanently `Pending` during routine node-pool maintenance.
- `readOnlyRootFilesystem: true` with a single `/tmp` emptyDir exception — required by the namespace's Pod Security "restricted" profile, and independently justified since Phase 4 already guarantees the app never needs to write to its own container filesystem.
- Plain numbered YAML manifests, not Helm/Kustomize — appropriate for one Deployment/one environment today; revisit if a second microservice or multiple environments make the duplication cost exceed a templating layer's complexity cost.
- `maxUnavailable: 0, maxSurge: 1` rolling strategy — true zero-downtime, safe specifically because the app is stateless (Phase 4): old and new Pods serving traffic simultaneously never causes a consistency problem.
- PodDisruptionBudget uses `minAvailable: 2` (an absolute floor), not `maxUnavailable`, so the guarantee holds regardless of what the HPA has scaled `replicas` to at the moment maintenance happens.
- NetworkPolicy default-deny-all + explicit allow-lists (GCLB health-check ranges, same-namespace Pods, DNS, Cloud SQL, Memorystore, Google APIs via Private Google Access) — requires Dataplane V2 enabled at cluster creation; **the Cloud SQL/Memorystore CIDR blocks in `11-networkpolicy.yaml` are placeholders** (`10.0.0.0/24`) that must be replaced with the real private-services-access ranges before this policy is applied to a real cluster, or all DB/Redis egress will be silently blocked.
- Docker: `docker/Dockerfile` is now the **single canonical Dockerfile** for both `docker-compose.yml` and every GKE image — the previous root-level single-stage `dockerfile` (no multi-stage, no non-root user) was deleted this phase to remove the duplicate-source-of-truth risk.
- CI/CD, a monitoring/observability stack, Pub/Sub, background workers, chunked uploads, multi-region, and disaster recovery are all **explicitly out of scope** this phase — only seams were prepared (Prometheus scrape annotations, the documented CI/CD pipeline shape in README §12) — see that section for exactly what was and wasn't done.

## Phase 6 Design Decisions (see README §13 for full detail)

- Temp-object-per-chunk + GCS Compose, NOT a single `Blob.create_resumable_upload_session()` — a single resumable session is sequential/single-writer (concurrent writes corrupt it, confirmed via GCS client-library issue trackers), so it can't satisfy "parallel chunk upload." Compose is GCS-native (not an invented distributed-storage mechanism), capped at 32 sources/call, so >32 chunks compose recursively in batches.
- Chunk bytes still transit FastAPI (`PUT /uploads/{id}/chunks/{n}`) rather than a client-direct-to-GCS handoff — dictated by the phase's own endpoint contract. Memory safety comes from never buffering more than one bounded chunk (`CHUNK_MAX_SIZE_BYTES`) and reading the raw ASGI stream directly (never Starlette's `UploadFile`, which can spool to local disk) — not from bypassing the app.
- `uploaded_bytes` is never updated via concurrent read-modify-write increments (a lost-update race under parallel chunk uploads) — progress is a live `SUM()` aggregate over VERIFIED chunks, computed on every read; the column itself is written exactly once, atomically, at completion.
- Duplicate-chunk prevention's real guarantee is a DB `UniqueConstraint(upload_id, chunk_number)`, with `UploadChunkRepository.create_or_get_existing` attempting the insert inside a SAVEPOINT so a losing race doesn't abort the whole request transaction — a per-chunk Redis lock only makes the race rare, it isn't the safety mechanism itself.
- All 9 new Phase 6 exceptions subclass an already-registered base (`NotFoundException`/`ConflictException`/`ValidationException`/`NimbusFSException`) — FastAPI/Starlette resolve handlers via MRO walk, so this needed **zero new handler functions and zero `main.py` changes**, same technique Phase 2's `FolderNotFoundException` already used.
- Redis-unavailable handling: `ChunkedUploadService._guarded_lock` translates an infrastructure failure at lock ACQUISITION into `ServiceUnavailableException` (503) — but deliberately does NOT wrap the work done *inside* a successfully-held lock, so a GCS/validation failure there still surfaces as its own real exception type, not misclassified as a coordination problem. Read-only endpoints never acquire a lock and keep working with Redis down.
- `retry_async` for per-chunk GCS uploads (cheap to retry a few times) vs. `CircuitBreaker` for the Compose call at completion (expensive to retry a multi-stage compose; fail fast once GCS is clearly unhealthy instead) — a deliberate split across two different Phase 4 primitives, not both stacked on everything.
- No content-dedup extension to the chunked path this phase — `actual_checksum` is computed and stored (parity with Phase 3), but a freshly-composed object is never checked against `FileMetadataRepository.get_by_checksum` the way Phase 3's single-shot upload is. Left as a clean, explicitly-scoped-out future addition, not a silently-skipped one.
- Known, acknowledged gap: a session that crashes (process death, not just a request exception) mid-`COMPLETING` — after Compose started but before the `except Exception: status = FAILED` handler could run — is left stuck in `COMPLETING` with no automatic recovery this phase (no background workers allowed). Requires operator intervention or a future reconciliation job; documented as a real limitation, not swept under the rug (see README §13's "Advanced" interview question).

## Phase 5 Verification Caveat

No real GKE cluster (or `kind`/`minikube`) was available in this session/environment (`docker ps` failed — Docker Desktop wasn't running), so **the `k8s/` manifests were validated syntactically only**: `python -c "import yaml..."` confirmed all 16 files parse as valid YAML (`k8s/11-networkpolicy.yaml` and `k8s/04-rbac.yaml` are correctly multi-document), and `bash -n` confirmed all 3 `scripts/k8s-*.sh` files are syntactically valid shell. **Nothing was applied to a live cluster; no manifest has been confirmed to actually reconcile successfully against the real Kubernetes API** (e.g. whether every CRD field name/apiVersion is accepted by GKE's actual admission controllers, whether the Pod Security "restricted" profile accepts the Deployment's securityContext as written) — treat `k8s/` as a strong, carefully-reasoned first draft that still needs a real `kubectl apply --dry-run=server` (or a real deploy per `k8s/README.md`) before being trusted in production. If a future session has cluster access, running `./scripts/k8s-deploy.sh` and `./scripts/k8s-smoke-test.sh --full` end-to-end is the natural next verification step.

## Config (`.env.example`)

All Phase 1/2 vars (see README §15) plus Phase 3: `GCS_PROJECT_ID`, `GCS_BUCKET_NAME`, `GCS_CREDENTIALS_PATH` (leave unset outside local dev — ADC/Workload Identity is used instead), `SIGNED_URL_EXPIRATION_MINUTES`, `MAX_UPLOAD_SIZE_MB`, `ALLOWED_MIME_TYPES`, `BLOCKED_EXTENSIONS`. Plus Phase 4: `INSTANCE_ID`/`HOSTNAME` (leave commented out — generated/defaulted per process), `BUILD_VERSION`, `GIT_COMMIT`, `TRUSTED_PROXIES`, `IDEMPOTENCY_KEY_TTL_SECONDS`, `IDEMPOTENCY_LOCK_TIMEOUT_SECONDS`, `LOCK_DEFAULT_TTL_SECONDS`, `LOCK_ACQUIRE_TIMEOUT_SECONDS`, `LOCK_RETRY_INTERVAL_SECONDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `FAIL_FAST_ON_STARTUP`, `SHUTDOWN_GRACE_PERIOD_SECONDS`. Plus Phase 6: `CHUNK_MIN_SIZE_BYTES`, `CHUNK_MAX_SIZE_BYTES`, `CHUNK_DEFAULT_SIZE_BYTES`, `MAX_CHUNKS_PER_UPLOAD`, `MAX_CHUNKED_UPLOAD_SIZE_GB`, `UPLOAD_SESSION_EXPIRATION_MINUTES`.

## Tests

145/145 passing. Run with `pytest -v`. Phase 3 tests never touch real GCS — `tests/fakes/fake_gcs.py::FakeGCSClient` is wired in via `app.dependency_overrides[get_gcs_client]` in `conftest.py`'s `client` fixture. Phase 4 tests never touch real Redis the same way, via `tests/fakes/fake_redis.py::FakeRedisClient` + `app.dependency_overrides[get_redis]`. Phase 6 reuses both fakes (plus `FakeBlob.compose()`, added this phase). **Exception**: `/health` and `/ready` intentionally call the real `check_database_connection()`/`check_redis_connection()` (module-level engine/pool, not request-scoped overrides) so they report actual replica connectivity — their tests assert response *shape* only, not a specific healthy/unhealthy outcome, and `conftest.py` pins `RETRY_*` env vars low before `app.main` is imported so those tests don't pay multi-second real-backoff costs against an unreachable Postgres/Redis in a sandboxed run. Phase 6 adds one more such test-speed override: `CHUNK_MIN_SIZE_BYTES=1024` (production default is 1 MiB — full-size chunks in every test would be needlessly slow).

**Gotcha discovered and fixed during Phase 6**: SQLite (the test backing store) round-trips `DateTime(timezone=True)` values as **naive** datetimes, unlike Postgres — comparing them directly against `datetime.now(timezone.utc)` raises `TypeError: can't compare offset-naive and offset-aware datetimes`. `ChunkedUploadService._is_expired` normalizes with `.replace(tzinfo=timezone.utc)` when `tzinfo is None`. If a future phase adds more datetime comparisons against DB-loaded columns, expect this same trap.

## History: What Was Fixed (2026-08-04 session)

For the record — these are resolved, not open issues:
1. **8 files had committed merge-conflict markers** (`app/main.py`, `app/api/__init__.py`, `app/api/v1/__init__.py`, `app/services/auth_service.py`, `app/services/user_service.py`, `tests/test_health.py`, `requirements.txt`, `alembic.ini`) — resolved in favor of the HEAD/Phase-2 side.
2. **Orphaned legacy tree deleted**: `app/domain/`, `app/infrastructure/`, `app/api/dependencies.py`, flat legacy route files, `app/core/config.py`/`security.py`/`logging.py`/`exceptions.py`, `migrations/`, `tests/test_auth.py`, stray `.env .example` typo file.
3. **`app/repositories/file_metadata_repository.py` was an empty file** — rebuilt from its usage in `metadata_service.py`/`search_service.py`.
4. **`app/schemas/file_metadata.py` contained the wrong content** (a duplicate of `folder.py`) — rebuilt with the correct `FileMetadataCreate`/`Read`/`Update`/etc. matching the model and routes.
5. **Async ORM bug**: `AuditMixin.updated_at`'s server-side `onupdate=func.now()` caused `MissingGreenlet` crashes on any request that mutated then serialized a row mid-request (e.g. folder rename, metadata update). Fixed by switching to a Python-side `onupdate` callable.
6. Also found during Phase 3 test-writing: `object_name` was initially modeled `unique=True`, which broke content-deduplication (two rows can legitimately share one object). Removed the uniqueness constraint, kept a plain index.

## Breaking Change Note (Phase 4)

`GET /health`'s response shape changed: `version`/`environment` moved from top-level `data` fields to a nested `data.server` object (which also now includes `instance_id`, `hostname`, `process_id`, `build_version`, `git_commit`). `data.storage` was added. Any external client/dashboard parsing the old flat shape needs updating — this was accepted deliberately (Phase 4 is additive to the API surface everywhere else) since `/health` is an operational endpoint, not a versioned public contract.

## Suggested Next Steps

Resume roadmap work at **Phase 7** per README §22 "Future Roadmap" — the user plans to provide the Phase 7 prompt in a future session. Do not regenerate Phases 1–6; extend the existing codebase only.

Before building further:
- The Phase 5 GKE-deployment verification caveat is still open (no manifest applied to a real cluster yet) and unrelated placeholder values (`<PROJECT_ID>`, domain, image tag, `11-networkpolicy.yaml`'s Cloud SQL/Memorystore CIDRs) still need replacing before a real deploy — see `k8s/README.md`.
- Phase 6's known, deliberate gaps: no automatic reconciliation of an upload session stuck mid-`COMPLETING` after a process crash (needs a future background-worker phase); no content-dedup extension to the chunked-upload path; the k6/Locust load tests were written but **not actually run** in this session (no load-testing infrastructure available) — see `scripts/load-test/README.md` for how to run them when infrastructure is available.
- Phase 4 left read replicas, row-level optimistic locking, real rate limiting, Redis metadata caching, and OpenTelemetry as designed-but-not-wired; Phase 5 left CI/CD, a monitoring stack, and multi-region/DR as documented-but-not-built. Revisit any of these only if a future phase's prompt actually calls for them — don't retrofit speculatively.
