# NimbusFS — Project Context

Purpose of this file: give a fresh AI session (or human) full context on this project in one read, without needing to re-explore the codebase from scratch. Written 2026-08-04; updated 2026-08-05 after completing Phase 4; updated 2026-08-08 after completing Phase 5; updated 2026-08-10 after completing Phase 6; updated 2026-08-15 after completing Phase 7; updated 2026-08-18 after completing Phase 8; updated 2026-09-01 after completing Phase 9; updated 2026-09-02 to record the canonical remote.

## Git Remote

As of 2026-09-02, this project's code is to be pushed to **https://github.com/adityagrawal45/distributed-storage-platform** — all commits from this point forward go there. (As of this note, the local working copy at `nimbusfs-phase1/` was not yet an initialized git repo — no local history existed to push.)

## Current Status: Phases 1–9 complete, repo healthy

The repo previously had **committed, unresolved Git merge-conflict markers** in 8 files (from a bad merge, `086377c "Merged existing repository"`) plus a parallel orphaned legacy implementation tree. **All of that has been resolved** — see "History: What Was Fixed" below for the record. As of now:

- `app.main` imports cleanly, the app starts, all routes are live.
- Full test suite: **416/416 passing** (246 Phases 1-7 + 164 Phase 8 + 6 Phase 9), against in-memory SQLite (`aiosqlite`) — no external services needed to run `pytest`. `/health`/`/ready` deliberately check *real* DB/Redis connectivity (see `app/database/session.py`/`redis.py`), so those two routes' test assertions are shape-only, not "must be healthy" — see `tests/conftest.py` for how the suite still stays fast without real infra. Phase 5 added no application code (manifests/Docker/scripts only); Phase 6 added chunked/resumable uploads (41 tests); Phase 7 added the Redis caching/coordination layer (101 tests); Phase 8 added the event-driven layer (164 tests); Phase 9 added the reconciliation service (6 tests) — zero regressions against every prior phase throughout.
- Phase 5's `k8s/` manifest set + `scripts/k8s-*.sh` runbook scripts were verified syntactically only (`python -c "import yaml..."`, `bash -n`) — **not** applied against a real GKE cluster in this session (none available); see "Phase 5 Verification Caveat" below. Phase 6 touched no `k8s/` files; Phase 7 touched exactly one (`k8s/05-configmap.yaml`, additive keys only). Phase 8 added six new manifests (`16-worker-serviceaccounts` through `21-deployment-notification-worker`) and extended `05-configmap.yaml` + `k8s/README.md`. Phase 9 added two new manifests (`22-cronjob-reconciliation`, `23-pdb-workers`), added `topologySpreadConstraints` to the API + all 4 worker Deployments, bumped `outbox-publisher`/`notification-worker` from 1→2 replicas, added a 5th worker KSA/GSA + RoleBinding for the reconciliation job, and extended `05-configmap.yaml` again — all validated by YAML parse only, **never applied to a cluster**, so the Phase 5 caveat now covers a larger surface than before.
- No known unresolved conflicts, no orphaned legacy code, no stray env files.

## What NimbusFS Is

A **cloud-native distributed file storage platform** (Google-Drive-style) built with Python, FastAPI, PostgreSQL, and Google Cloud Storage. Built in phases (~15-phase roadmap). Currently implemented:

- **Phase 1**: user registration/auth (JWT access+refresh, role-based access)
- **Phase 2**: folder hierarchy, file metadata, soft-delete/trash, versioning, search & pagination
- **Phase 3**: real file upload/download via Google Cloud Storage, signed URLs, streaming downloads with Range support, SHA-256 content-based duplicate detection, upload/metadata rollback consistency
- **Phase 4**: distributed backend architecture — stateless multi-replica design, correlation/trace/server-ID propagation + structured logging, `/health`+`/ready`+`/live` endpoints, fail-fast startup + graceful shutdown lifecycle, Redis-backed distributed locks, Redis-backed `Idempotency-Key` support on `POST /files/upload`, DB/Redis/Storage retry-with-backoff, a circuit breaker primitive, trusted-proxy/forwarded-header handling, a rate-limit middleware placeholder.
- **Phase 5**: Kubernetes deployment on GKE — full manifest set in `k8s/` (Namespace, ResourceQuota/LimitRange, ServiceAccount+RBAC via Workload Identity, ConfigMap/Secret, Deployment with startup/readiness/liveness probes + rolling strategy + pod anti-affinity/node affinity, ClusterIP Service with container-native load balancing, HPA 3→10 on CPU+memory, PodDisruptionBudget, default-deny NetworkPolicy, GKE Ingress + BackendConfig + FrontendConfig + ManagedCertificate), a hardened multi-stage non-root Dockerfile (now the single canonical one for dev + prod), and `scripts/k8s-deploy.sh`/`k8s-smoke-test.sh`/`k8s-scale-demo.sh`.
- **Phase 6**: large-file chunked/resumable uploads — a second upload path (`/api/v1/uploads/*`, distinct from Phase 3's `/files/upload`) supporting arbitrarily large files via independent-temp-object-per-chunk + GCS-native Compose (NOT a single GCS resumable session — that's sequential-only and can't support genuine parallel chunk upload, see README §13 for the full research-backed rationale), explicit `UploadSession`/`UploadChunk` state machine, resumability (lazy expiration, live-computed progress), per-chunk + final SHA-256 checksums, Redis-lock-guarded concurrency control with a real DB unique constraint as the ultimate duplicate-chunk guarantee, `Idempotency-Key` reuse (same `IdempotencyService` as Phase 4) on initiate/complete, and k6/Locust load-test scripts. No Pub/Sub, background workers (including no automatic reconciliation of a session stuck mid-`COMPLETING` after a process crash — see README §13's "Advanced" interview question), full Redis caching, disaster recovery, multi-region, CI/CD, full observability stack, or content-dedup extension to the chunked path — those are future-phase/explicitly-out-of-scope.
- **Phase 7**: distributed Redis caching & coordination — cache-aside reads (user profile, folder metadata/children/breadcrumbs, file metadata, version history, search pages) behind a single `CacheService` gateway with **single-flight stampede protection**; a `CacheKeyBuilder`/`CacheSerializer`/`CachePolicy`/`CacheInvalidator` split; JSON-with-a-schema-version serialization (never pickle); per-entity config-driven TTLs; `SCAN`-based (never `KEYS`) invalidation fan-out on every write; a `DistributedLockService` facade extending Phase 4's lock with bounded/jittered acquire, ownership introspection and strict release; and **real rate limiting** (atomic Lua token bucket, per-route-category budgets, 429 + `Retry-After`) replacing Phase 4's no-op middleware placeholder. Postgres stays authoritative for metadata and GCS for bytes — every Redis failure is logged and degraded to a cache miss, never raised. No Pub/Sub, background workers, Prometheus/OpenTelemetry stack, negative caching, cache warming, or post-commit invalidation — see Phase 7 Design Decisions below for the honest gap list.
- **Phase 8**: event-driven architecture — Google Cloud Pub/Sub plus a **transactional outbox**, moving thumbnailing and notifications off the upload request entirely. The API now writes an `OutboxEvent` row in the *same transaction* as the business data it describes (one commit, one atomic outcome — solving the dual-write problem without any new transaction-management code, by exploiting the Unit of Work `get_db` already provided). A standalone `outbox-publisher` worker polls those rows (`FOR UPDATE SKIP LOCKED`), publishes to one of three domain topics, and marks each row published with a per-row commit. Three idempotent consumers then run as separate processes: the **file-processing worker** (verifies the GCS bytes really landed, cross-checks size/content-type, fans out), the **thumbnail worker** (Pillow, 4 supported raster formats behind an allow-list checked before any download or decode, writes `thumbnails/{file_id}.png` and sets `FileMetadata.thumbnail_object_name`), and the **notification worker** (renders and persists a `Notification` row via a `NotificationSender` seam — `LoggingNotificationSender` is the only implementation, no real email provider). Twelve-value `EventType` catalog with a typed `EventEnvelope` (correlation/causation chaining read from the `structlog` contextvars `RequestContextMiddleware` already binds). At-least-once end to end, made effectively-once by `ProcessedEvent`'s `UniqueConstraint(event_id, consumer_name)` — the pre-check is an optimization, the constraint is the guarantee — plus **deterministic UUIDv5 derived event IDs** so a retried fan-out cannot defeat downstream deduplication. `PUBSUB_ENABLED` defaults to **false**, so the whole integration ships dark: outbox rows are written transactionally and simply never leave Postgres until the switch is flipped. Every worker is the same container image started with a different `python -m app.workers.<name>`; four Kubernetes Deployments, each with its own scoped service account, liveness-only heartbeat-file probe, and no Service/Ingress/readiness probe. See README §15 and `docs/event-driven-architecture.md`. No HPA/backlog autoscaling, no real email provider, no DLQ replay tooling, and **nothing in this phase was ever run against real infrastructure** — see the honest gap list in "Phase 8 Design Decisions" below.

- **Phase 9**: high availability & disaster recovery — `topologySpreadConstraints` added to the API and all four Phase 8 worker Deployments (zone-level `maxSkew: 1`, additive to Phase 5's soft `podAntiAffinity`, not a replacement for it); `outbox-publisher`/`notification-worker` bumped from 1→2 replicas specifically for zone-redundancy (each Deployment's own header comment carries the reasoning); a `minAvailable: 1` PodDisruptionBudget added for all four worker Deployments now that each runs >=2 replicas; a new read-only `ReconciliationService`/`reconciliation_job.py`, run every 6 hours via `k8s/22-cronjob-reconciliation.yaml` under its own least-privilege KSA/GSA, that keyset-paginates every non-deleted `upload_status=COMPLETED` `FileMetadata` row and flags one it can't find the backing GCS object for — it has **no delete/update code path anywhere in its call graph**, proven by `tests/test_reconciliation.py::test_never_mutates_or_deletes_anything`. Everything else this phase produced is design/documentation, not code: an availability target (99.9%) and RTO/RPO targets (<4h/<1h) with derivations, Cloud SQL Regional-HA and Memorystore Standard-tier configuration guidance (not applied — no real instances existed to apply it to), a GCS durability/protection recommendation (regional bucket + a scheduled cross-region object-replication job, explicitly **not** a dual-region bucket — cost-aware, not the most expensive default), an active-passive warm-standby multi-region DR design with a manual failover runbook (active-active explicitly rejected — no requirement justifies solving multi-writer Postgres consistency), a failure matrix, a monitoring metric inventory, severity-tiered alerts, a cost comparison (no fabricated pricing), and chaos-testing procedures for all 13 requested scenarios labeled LOCAL/STAGING/PRODUCTION. See README §16 and `docs/high-availability.md`/`docs/disaster-recovery.md`/`docs/failure-testing.md`/`docs/backup-restore.md` for the full depth. **Nothing in Phase 9 was run against real infrastructure** — no real GKE cluster, Cloud SQL instance, or Memorystore instance was available in this session either, so every HA/DR number in this phase is a justified target (DESIGNED), not a drill result (MEASURED) — see the honest gap list in "Phase 9 Design Decisions" below.

**Not yet built** (future phases, per README §24): sharing/permissions between users, virus scanning (placeholder only), full-text content search, content-dedup extension to chunked uploads (Phase 6), CI/CD automation (Phase 5 only documented the intended shape), Terraform, observability/OpenTelemetry tracing (Phase 5 only prepared Prometheus annotations), backlog-based worker autoscaling, orphaned-GCS-object detection (Phase 9's reconciliation job deliberately covers only the other, more dangerous direction), and reconciliation of upload sessions stuck mid-`COMPLETING` (Phase 8's workers make it *possible*, but no such job was written — distinct from Phase 9's Postgres↔GCS drift reconciliation). (Real rate limiting and Redis metadata caching were on this list until Phase 7 shipped them; Pub/Sub background workers and thumbnails until Phase 8 did; multi-zone HA and DR design until Phase 9 did.)

## Tech Stack

| Concern | Choice |
|---|---|
| Framework | FastAPI 0.115.x + Uvicorn (ASGI) |
| Validation/config | Pydantic v2 + pydantic-settings |
| Database | PostgreSQL 16 (`postgres:16-alpine` in docker-compose) |
| DB driver | `asyncpg` (app), `psycopg2-binary` (Alembic, sync) |
| ORM | SQLAlchemy 2.0, async, `Mapped`/`mapped_column` style |
| Migrations | Alembic 1.14 (`alembic/versions/`) |
| Cache | Redis 7 / Cloud Memorystore — Phase 4: health checks, distributed locks, idempotency keys; Phase 7: metadata caching + rate-limit token buckets. **Never authoritative, never stores file bytes.** |
| Auth | JWT (`python-jose[cryptography]`) + `passlib[bcrypt]`/`bcrypt`; OAuth2 Password flow with access+refresh token rotation |
| Logging | `structlog` (structured, JSON-toggleable via `LOG_JSON`) |
| Cloud Storage | `google-cloud-storage` SDK, private bucket, V4 signed URLs, MIME sniffing via `filetype` |
| Testing | `pytest` + `pytest-asyncio` (`asyncio_mode = auto`), `httpx.AsyncClient`, in-memory SQLite (`aiosqlite`), hand-written `FakeGCSClient` (`tests/fakes/fake_gcs.py`) and `FakeRedisClient` (`tests/fakes/fake_redis.py` — Phase 7: + hashes/SCAN/INCR/EXPIRE/TTL, real token-bucket arithmetic, a controllable clock, and failure injection) — no external services, real GCS, or real Redis needed |
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
    distributed_lock.py        Phase 4: DistributedLock/DistributedLockFactory — Redis SET NX PX + Lua-checked release;
                                Phase 7: + acquire_with_timeout (bounded/jittered), owns(), release(strict=),
                                and DistributedLockService (facade: timeout policy + contention-vs-infra separation)
    rate_limiter.py            Phase 7: RateLimiter — atomic Lua token bucket, per-category budgets,
                                configurable fail-open/fail-closed. Replaces the Phase 4 no-op placeholder's job.
    cache/keys.py              Phase 7: CacheKeyBuilder — WHAT a key is called (namespacing, collision safety,
                                hashed variable-length components, SCAN patterns for invalidation)
    cache/serializer.py        Phase 7: CacheSerializer — HOW a value is encoded (JSON not pickle; versioned
                                envelope; an unknown schema version is a MISS, never a crash)
    cache/policy.py            Phase 7: CacheEntity/CachePolicy — HOW LONG a value lives (per-entity TTLs from Settings)
  events/                      Phase 8: envelope.py (EventEnvelope pydantic model, to_pubsub_message()),
                                topics.py (EventType enum, EVENT_TYPE_TO_TOPIC routing table), publisher.py
                                (EventPublisher — wraps the sync google-cloud-pubsub client via run_in_executor so
                                it never blocks the event loop; no-ops when PUBSUB_ENABLED=false), emitter.py
                                (OutboxEmitterMixin — the one place the "build envelope, insert outbox row, never
                                raise" logic lives, shared by all 4 emitting services instead of 4 copies)
  workers/                     Phase 8: standalone processes, each runnable as `python -m
                                app.workers.<name>` — separate from the FastAPI process entirely. base.py
                                (BaseWorker: Pub/Sub StreamingPullFuture, sync-callback-to-asyncio bridge, per-
                                message contextvar binding, ProcessedEvent idempotency check, ack/nack/duplicate-
                                race handling), runtime.py (GracefulShutdownMixin + heartbeat-file helper, shared
                                by every worker including the non-BaseWorker outbox publisher), outbox_publisher.py
                                (polls OutboxEvent, publishes, marks published/failed with backoff — its own
                                worker, not folded into a consumer, since its IAM/failure-mode is unrelated),
                                file_processing_worker.py (validates uploaded GCS objects, publishes
                                thumbnail.requested/notification.requested directly — worker-to-worker, no outbox,
                                since there's no competing Postgres write to stay atomic with at that point),
                                thumbnail_worker.py (consumes thumbnail.requested, Pillow-based, allow-list
                                checked BEFORE download/decode, DB write LAST so a crash never leaves a dangling
                                thumbnail pointer), notification_worker.py (consumes notification.requested off
                                its own topic — egress isolation, so a wedged provider can never backpressure
                                file processing; constructs its sender PER MESSAGE so a session never leaks
                                across messages), Phase 9: reconciliation_job.py (a one-shot batch job, NOT a
                                BaseWorker subscriber and NOT a long-running loop — meant to be invoked by a
                                Kubernetes CronJob; exit code 0/1/2 = clean/issues-found/scan-incomplete; makes
                                zero INSERT/UPDATE/DELETE calls anywhere in its call graph)
  database/
    session.py                  async engine/session, declarative Base; Phase 4: pool tuning, retry-wrapped
                                 check_database_connection(), run_with_deadlock_retry(), close_db_engine(),
                                 documented (not-yet-wired) read/write-separation + optimistic-locking design
    redis.py                    redis pool + check_redis_connection() (Phase 4: retry-wrapped), close_redis_pool()
    gcs.py                      GCS client factory (ADC in prod, key file path in dev) — Phase 3;
                                 Phase 4: check_storage_connection() bucket-verification health check
  dependencies/
    auth.py                     get_current_user, CurrentUser, require_role(); Phase 4: stashes request.state.user_id.
                                 NOTE: deliberately still hits Postgres every request (NOT cached) — Phase 1 chose
                                 that so deactivation takes effect immediately; Phase 7 preserved it on purpose
    rate_limit.py               Phase 7: rate_limit(category) FastAPI dependency factory + get_rate_limiter provider
                                 (lives here, not providers.py, to keep the import graph acyclic; providers.py
                                 re-exports it). Identity = JWT `sub` decoded locally (no DB hit), else client IP.
    providers.py                DI wiring for repositories/services, incl. StorageServiceDep/FileUploadServiceDep/
                                 GCSClientDep, Phase 4: DistributedLockFactoryDep/IdempotencyServiceDep, and
                                 Phase 6: UploadSessionRepositoryDep/UploadChunkRepositoryDep/ChunkedUploadServiceDep
  models/                       user.py, refresh_token.py, folder.py, file_metadata.py (+Phase 3 storage columns,
                                 +Phase 8: nullable thumbnail_object_name), file_version.py, mixins.py,
                                 Phase 6: upload_session.py (AuditMixin), upload_chunk.py (own Python-side updated_at
                                 onupdate — see its docstring for the History #5 bug it avoids); Phase 8 (all
                                 three no-mixins per the UploadChunk precedent): outbox_event.py, processed_event.py,
                                 notification.py
  repositories/                 base.py (+Phase 6: flush() helper) + one repo per entity; file_metadata_repository.py
                                 has get_by_checksum/object_name_in_use for dedup; Phase 6: upload_session_repository.py
                                 (get_owned), upload_chunk_repository.py (create_or_get_existing — SAVEPOINT-guarded
                                 insert, sum_verified_bytes, list_verified_ordered, delete_all_for_upload); Phase 8:
                                 outbox_repository.py (fetch_pending_batch with FOR UPDATE SKIP LOCKED,
                                 mark_published/mark_failed with backoff), processed_event_repository.py
                                 (has_processed pre-check + SAVEPOINT-guarded record() — the real idempotency guarantee);
                                 Phase 9: file_metadata_repository.list_completed_batch (keyset pagination by id,
                                 never OFFSET, for the reconciliation job to walk the whole table in bounded chunks)
  services/                     auth_service.py, user_service.py, folder_service.py, metadata_service.py,
                                 search_service.py, trash_service.py, version_service.py,
                                 storage_service.py (Phase 3, GCS wrapper — ONLY module importing google.cloud.storage;
                                 Phase 6: +compose_objects [multi-stage GCS Compose], delete_many, compute_object_checksum),
                                 file_validation_service.py (Phase 3), file_upload_service.py (Phase 3 orchestrator),
                                 idempotency_service.py (Phase 4: Redis-backed Idempotency-Key contract, reused
                                 unchanged by Phase 6), chunked_upload_service.py (Phase 6: the whole chunked-upload
                                 orchestration — see its long module docstring for the full design writeup),
                                 Phase 7: cache_service.py (THE only gateway to Redis-as-cache — get/set/delete/
                                 exists/expire/increment/get_or_set/invalidate/scan, every Redis failure logged +
                                 degraded, single-flight stampede protection) and cache_invalidator.py
                                 (operation-named key fan-out; deletes, never updates).
                                 Phase 7 also added optional `cache=`/`invalidator=` kwargs to user_service,
                                 folder_service, metadata_service, search_service, version_service,
                                 file_upload_service and chunked_upload_service — all keyword-only with None
                                 defaults, so every pre-existing direct construction still works unchanged.
                                 Phase 8: thumbnail_service.py (Pillow decode/resize behind an explicit 4-type
                                 allow-list; Image.open(formats=...) pins the decoder so a mislabelled file is
                                 rejected rather than sniffed; deterministic output object name so regeneration
                                 overwrites instead of orphaning) and notification_service.py (NotificationSender
                                 ABC + LoggingNotificationSender — writes the Notification row and logs "would
                                 send email (stub)"; flushes, never commits, so the row and its ProcessedEvent
                                 row commit together — plus render_notification(), which turns an envelope into
                                 an unsaved Notification and raises NonRetryableEventError on a payload this
                                 codebase's own producer should never have emitted); Phase 9: reconciliation_service.py
                                 (read-only Postgres<->GCS consistency check — walks FileMetadata in batches via the
                                 repository above, confirms each object_name exists in GCS via StorageService.
                                 get_blob_metadata; NO delete/update code path anywhere in it, by construction, not
                                 by a flag — see its module docstring for why only one drift direction is covered)
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
                                 zero main.py changes, see Phase 6 Design Decisions below; Phase 7: CacheError/
                                 CacheConnectionError/CacheSerializationError/DistributedLockError/
                                 LockAcquisitionTimeout/LockOwnershipError/RateLimitExceeded — all subclass a
                                 registered base EXCEPT RateLimitExceeded, which needs a 429 + Retry-After no
                                 existing handler can produce),
                                 handlers.py (Phase 1-4 handlers + exactly ONE new Phase 7 handler:
                                 rate_limit_exceeded_exception_handler)
  logging/logger.py             structlog config, get_logger()
  middleware/                   request_context.py (Phase 4: adds correlation_id/trace_id/server_id, more response
                                 headers), security_headers.py, proxy_headers.py (Phase 4: TrustedProxyMiddleware),
                                 rate_limit.py (Phase 4: RateLimitPlaceholderMiddleware — explicit no-op;
                                 Phase 7: now RateLimitHeadersMiddleware, which only REPORTS the decision the
                                 rate_limit(...) dependency made, as X-RateLimit-* headers. The old name is kept
                                 as an alias. Enforcement is a route dependency, not middleware — see below.)
  utils/                        path_utils.py (materialized-path helpers), response.py
alembic/versions/               0001_initial, 0002_metadata, 0003_storage (adds GCS columns to file_metadata),
                                 Phase 6: 0004_chunked_uploads_add_upload_sessions_and_chunks (creates upload_sessions,
                                 upload_chunks + their 2 enum types) — no migration in Phase 4/5 (no model changes);
                                 Phase 8: 0005_events_add_outbox_processed_and_notifications (creates
                                 outbox_events/processed_events/notifications + 2 enum types + additive
                                 FileMetadata.thumbnail_object_name column) — **HAS NEVER BEEN RUN against a real
                                 Postgres**, in either Phase 8 session. Verified only by import/model-definition
                                 correctness and the SQLite-backed suite. Run `alembic upgrade head` /
                                 `downgrade -1` / `upgrade head` against a real Postgres before trusting it
tests/
  conftest.py                  client/db fixtures + fake_gcs_client/fake_redis_client fixtures (override
                                get_gcs_client/get_redis for every test); Phase 4: pins RETRY_* env vars low
                                before app import so /health-touching tests stay fast; Phase 6: also pins
                                CHUNK_MIN_SIZE_BYTES=1024 (same pattern) so chunk tests stay fast
  fakes/fake_gcs.py             FakeGCSClient/FakeBucket/FakeBlob — in-memory GCS stand-in, no real network calls;
                                Phase 6: +FakeBlob.compose() (real byte-concatenation, not a call-count mock)
  fakes/fake_redis.py           Phase 4: FakeRedisClient — in-memory stand-in for the redis.asyncio surface
                                 NimbusFS actually uses (set/get/delete/eval/ping), no real Redis needed;
                                 Phase 7: EXTENDED (not replaced) with hash storage, exists/expire/ttl/incrby,
                                 scan_iter, real token-bucket arithmetic for the rate-limit Lua script,
                                 FakeClock (controllable clock — TTL/lock-expiry/bucket-refill tests are instant
                                 and deterministic, no sleeps), and start_failing(*commands, after=N) failure
                                 injection — which is what makes every degradation assertion genuine
  test_health/registration/login/protected_routes/folders/metadata/search.py   Phase 1/2 tests
  test_file_storage.py          Phase 3 tests (upload/download/range/signed-url/replace/permanent-delete/dedup/rollback/failure)
  test_distributed.py           Phase 4 tests (idempotency, distributed locks, retry, circuit breaker, correlation
                                 IDs, graceful degradation, concurrency) — see README §22 for the full list
  test_chunked_upload.py        Phase 6: 41 tests (initiate/chunk-upload/resume/expiration/cancellation/completion/
                                 idempotency/concurrency/ownership/state-machine/DB-GCS-Redis-failure/size-validation)
                                 — see README §13 "Testing" for the full list
  test_caching.py               Phase 7: 72 tests (key builder, serializer, cache primitives, cache-aside +
                                 50-request stampede assertion, Redis-failure degradation on every op, invalidator
                                 fan-out, write guard, distributed locks incl. the lost-lock guard, and end-to-end
                                 cache/DB consistency through the HTTP API incl. a cross-user 404 auth test)
  test_rate_limiting.py         Phase 7: 29 tests (within/over budget, exact Retry-After, refill over controlled
                                 time, capacity capping, 20-concurrent-one-bucket, identity/category isolation,
                                 reset/peek, fail-open vs fail-closed, forged-token IP fallback, 429 HTTP contract)
  fakes/fake_pubsub.py          Phase 8: FakePubSubClient — in-memory per-topic message list, fake
                                 message wrapper exposing ack()/nack() as recording stubs, wired into conftest.py
                                 exactly like fake_gcs_client/fake_redis_client
  test_events_envelope.py       Phase 8: envelope field defaults/types, to_pubsub_message() round-trip, every
                                 EventType has a topic-routing entry
  test_event_publisher.py       Phase 8: publish-when-enabled, no-op-when-disabled, executor-wrapping doesn't block
  test_outbox_repository.py     Phase 8: fetch_pending_batch ordering, mark_published/mark_failed transitions,
                                 next_attempt_at backoff math
  test_event_emission.py        Phase 8: every emitting service's mutating methods produce the right OutboxEvent
                                 row with the right aggregate_type/aggregate_id/payload, and existing call sites
                                 with no outbox= passed still work unmodified
  test_outbox_publisher_worker.py  Phase 8: polling picks up PENDING rows, publish failure marks FAILED with
                                 incremented attempt_count, a FAILED row past next_attempt_at is retried
  test_base_worker.py           Phase 8: ack-after-success, nack-on-retryable, ack+FAILED-on-non-retryable,
                                 duplicate-delivery pre-check skip, losing-idempotency-race still acks
  test_file_processing_worker.py  Phase 8: GCS-object validation, thumbnail.requested only for supported content
                                 types, notification.requested always published
  test_thumbnail_worker.py      Phase 8: 4 supported raster formats succeed and write to GCS + update
                                 thumbnail_object_name; every other content type raises NonRetryableEventError
                                 without attempting to decode. Uses REAL Pillow on REAL generated bytes —
                                 mocking the decoder would test nothing, since the whole risk is what a decoder
                                 does with malformed input
  test_notification_worker.py   Phase 8: 17 tests — rendering (templates, fallback for an unknown
                                 notification_type, missing/malformed payload => NonRetryableEventError), the
                                 sender's flush-never-commit contract, and the worker's ack/dedup behavior
  test_events_integration.py    Phase 8: 4 tests — the WHOLE chain, nothing mocked between stages. Real HTTP
                                 upload -> PENDING OutboxEvent -> outbox publisher poll -> message on the fake
                                 broker -> file worker fan-out -> thumbnail rendered into FakeGCS +
                                 thumbnail_object_name set -> Notification row. Plus: a non-image upload skips
                                 thumbnailing entirely, the entire chain redelivered end-to-end changes nothing
                                 (one ledger row per consumer, one notification, one thumbnail object), and a
                                 mid-flight Pub/Sub outage leaves the event durable and replayable. Uses a
                                 shared-session factory so the workers see the request transaction's rows —
                                 documented in the module docstring, since cross-process transaction isolation
                                 is tested in test_event_emission.py instead
  test_reconciliation.py        Phase 9: 6 tests — clean state / missing-object flagged / soft-deleted rows
                                 skipped / pending-upload rows skipped / multi-batch keyset pagination walks
                                 every row / the service never mutates or deletes the row it flagged. Uses its
                                 own throwaway SQLite engine + FakeGCSClient, same pattern as
                                 test_thumbnail_worker.py's `worker_db`/`gcs` fixtures
k8s/                             Phase 5: Kubernetes manifests, numerically prefixed for apply order (00-namespace
                                  through 15-ingress) — see k8s/README.md for the full table + deployment runbook.
                                  Not pytest-testable (no cluster in this environment); validated via YAML parse +
                                  `kubectl apply --dry-run=client` guidance only, see "Phase 5 Verification Caveat" below.
                                  Untouched by Phase 6. Phase 8 added 16-worker-serviceaccounts.yaml (4 KSAs, one
                                  scoped GSA each — the per-worker IAM table is in that file's header),
                                  17-worker-rbac.yaml (4 RoleBindings onto the EXISTING nimbusfs-app-role — no new
                                  Role; the permissions that genuinely differ per worker are GCP IAM, not
                                  Kubernetes RBAC), and 18/19/20/21-deployment-*.yaml (outbox publisher, file
                                  worker, thumbnail worker, notification worker). 18's header carries the shared
                                  reasoning for all four; 20 is the outlier (1Gi memory, WORKER_CONCURRENCY
                                  overridden down to 3, longer termination grace). 05-configmap.yaml and
                                  k8s/README.md's apply-order table were extended, not duplicated. Phase 9 added
                                  topologySpreadConstraints (zone-level, maxSkew:1, additive to Phase 5's soft
                                  podAntiAffinity) to 07-deployment.yaml and 18-21; bumped 18/21 (outbox-
                                  publisher, notification-worker) from 1->2 replicas purely for zone-redundancy
                                  (each file's own header explains why, distinct from the Phase 8 throughput
                                  reasoning that set them to 1 in the first place); added a 5th worker KSA/GSA
                                  (nimbusfs-reconciliation-ksa) to 16-worker-serviceaccounts.yaml and its
                                  RoleBinding to 17-worker-rbac.yaml, read-only on both GCS and Cloud SQL — no
                                  write IAM grant exists for it, matching the service code having no delete path;
                                  added 22-cronjob-reconciliation.yaml (every 6h) and 23-pdb-workers.yaml
                                  (minAvailable:1 per worker, now that each runs >=2 replicas); extended
                                  05-configmap.yaml with RECONCILIATION_* keys.
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
  benchmark/benchmark_cache.py   Phase 7: A/B latency benchmark (p50/p90/p99) for three cached read endpoints,
                                  cache-on vs cache-off. Produces numbers; ships none.
  benchmark/README.md            Phase 7: runbook + a blunt "what NOT to conclude" section (chiefly: this measures
                                  latency at concurrency 1, while a cache's real job is shedding load under load)
docs/
  PHASE_7_REDIS_DESIGN.md        Phase 7: the standalone technical design doc — ASCII architecture diagrams,
                                  cache-aside/serialization/TTL/invalidation rationale, full race analysis,
                                  stampede + lock + rate-limit algorithm detail, GCP/Memorystore production
                                  architecture, a 12-row failure-scenario catalogue, and beginner->advanced Q&A
  event-driven-architecture.md   Phase 8: the standalone deep-dive — the full dual-write hazard analysis (all
                                  four orderings, why three are broken, and what the outbox does NOT buy), ASCII
                                  sequence diagrams for the happy path / Pub-Sub outage / consumer crash,
                                  ack-timing rationale + the complete ack/nack decision table, a
                                  retryable-vs-non-retryable classification table, a DLQ operational runbook
                                  (provisioning gcloud commands, triage SQL keyed on event_id/correlation_id,
                                  replay procedure), the no-ordering-keys decision written out with what would
                                  change it, the versioning contract and rollout ordering, a 16-row failure
                                  catalogue, and deeper interview Q&A
  high-availability.md           Phase 9: availability target + derivation, multi-zone GKE detail, Cloud SQL/
                                  Memorystore HA design, in-app Redis-failure handling (cross-referenced from
                                  Phase 7), Pub/Sub/worker resilience (cross-referenced from Phase 8), a full
                                  failure matrix, monitoring metric inventory + severity-tiered alerts, a
                                  security-during-failover review, a cost comparison (single-zone/multi-zone/
                                  multi-region, no fabricated pricing), design decisions, interview Q&A, and a
                                  completion checklist explicit about what is DESIGNED vs MEASURED
  disaster-recovery.md           Phase 9: RTO/RPO derivation, GCS durability/protection strategy + cross-region
                                  storage recommendation, a full data-consistency analysis across all 5 systems,
                                  the reconciliation design in full, an active-passive warm-standby multi-region
                                  DR design with alternatives compared, a manual failover runbook, DNS-failover
                                  caveats, secrets/IAM-for-DR guidance, failure scenarios narrated, interview
                                  Q&A, and a completion checklist
  failure-testing.md             Phase 9: environment-labeling scheme (LOCAL/STAGING/PRODUCTION), the Unit/
                                  Integration/Infrastructure/Failure test-category distinction with real
                                  examples from this repo, all 13 requested chaos-testing scenarios with
                                  commands and pass criteria, an RTO/RPO measurement methodology + record
                                  template (intentionally blank — no drill was run), and a catalogue of what
                                  the existing LOCAL/TEST suite already covers without any real infrastructure
  backup-restore.md              Phase 9: Cloud SQL backup/PITR configuration + gcloud commands, an executable
                                  7-step STAGING-only restore exercise with real curl/gcloud/python commands
                                  (including feeding the restored state into the Phase 9 reconciliation job as
                                  a real drift-detection test), an explicit refusal to fabricate a result, and
                                  a path toward future restore-test automation (not built this phase)
```

## API Surface (all under `/api/v1`)

Every response uses the standard envelope, `app/schemas/response.py::APIResponse[T]`: `{success, message, data, errors, timestamp, request_id}`.

**Phase 7 addition, API-wide**: every response now carries `X-RateLimit-Limit`/`X-RateLimit-Remaining` (and `X-RateLimit-Category` on limited routes); routes with no budget report `unlimited` rather than omitting the headers, exactly as the Phase 4 placeholder promised. Rate-limited routes are `POST /auth/login` (10/60s), `POST /auth/register` (5/300s), all of `/folders/*` and `/metadata/*` (300/60s, applied at ROUTER level so a new route can't be silently unprotected), `GET /metadata/search` (60/60s, stacked on top), `POST /uploads` (60/60s) and `POST /uploads/{id}/complete` (60/60s). Per-chunk `PUT /uploads/{id}/chunks/{n}` is deliberately unlimited. Over-budget requests get `429` in the standard envelope with a `Retry-After` computed from the real token deficit. Cached read paths (`GET /users/{id}`, `GET /folders/{id}`, `GET /folders`, `GET /folders/breadcrumb`, `GET /metadata/{id}`, `GET /metadata/{id}/versions`, `GET /metadata/search`) changed **no response shapes** — the services return the same Pydantic schemas the routes were already building.

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

## Phase 7 Design Decisions (see README §14 for full detail, docs/PHASE_7_REDIS_DESIGN.md for the deep dive)

- The governing invariant: **Postgres owns metadata, GCS owns bytes, Redis owns nothing.** Flushing Redis at any moment must cost only latency. Two code-level enforcements, not just prose: `CacheSerializer.encode` raises if handed `bytes` (file content physically cannot be written to Redis by this codebase), and every `CacheService` method catches every Redis exception, logs it, and returns the "as if the cache did not exist" answer.
- **JSON, never pickle.** Three independent disqualifiers, any one sufficient: `pickle.loads` on a shared, network-reachable, multi-writer datastore is arbitrary code execution; pickle encodes fully-qualified class paths, so a routine `FolderRead` rename breaks every cached entry *mid-rolling-deploy* while both builds read the same Redis; and pickle is unreadable from `redis-cli` during an incident.
- **Every cached value carries a schema version** (`CACHE_SCHEMA_VERSION` in the envelope `{"v","ts","d"}`), and an unrecognized version is treated as a **cache miss**, not an error. This is exactly what makes a cache-format change safe to deploy — the worst outcome is one cold period instead of a partial outage. The same path absorbs malformed JSON and corrupt entries.
- **One Redis pool, not two** — Phase 7 reuses Phase 4's `app/database/redis.py` pool (with the previously-hardcoded 5s timeouts now config-driven and tightened to 2s, plus `retry_on_timeout` and `health_check_interval`). One place to size, one place to observe, one bounded connection ceiling against Memorystore (`REDIS_MAX_CONNECTIONS` x replica count).
- **All Redis-as-cache access funnels through `CacheService`; `redis.asyncio` is imported by exactly three modules** (the pool, `CacheService`, `RateLimiter`). Scattered `await redis.get(...)` at call sites is how a *cache* outage becomes an *application* outage — every site would have to independently get error handling right, and they never all do.
- **Delete on write, never update.** Write-through cache updates can be applied in the opposite order to their DB commits under concurrency, leaving the cache permanently disagreeing with Postgres with no TTL-independent way to notice. Deleting is idempotent and order-independent; the loser of any race just causes one extra read.
- **Single-flight stampede protection with a BOUNDED follower wait**, and an explicit "far fewer DB hits than requests" guarantee rather than "exactly one". On a miss, one request wins a short-TTL Redis lock, re-checks the cache (the double-check is what makes it correct rather than lucky), loads, and publishes; followers poll briefly, then **read through to Postgres anyway**. Unbounded waiting would convert one slow query into worker-pool exhaustion — strictly worse than the stampede it prevents. Redis failure during lock coordination is explicitly non-fatal here (the lock is a performance optimization), unlike `DistributedLockService.guard`, which raises.
- **Resource-scoped keys + an ownership re-check, not caller-scoped keys** — for folders/files/users the cached payload carries `owner_id` and the service re-applies exactly the filter the repository's WHERE clause would have, raising the same **404** (never 403, so IDs stay unguessable). Search is the one entity where caller-scoping is correct rather than a pessimization, because a result set has no owner field to re-check. Authorization is re-derived on every cached read; a *decision* is never cached. There is a test where user B tries to read a folder user A just warmed into the cache and gets a 404.
- **The user cache is deliberately NOT on the auth path.** `get_current_user` still reads Postgres every request, preserving Phase 1's "deactivation takes effect immediately" property. Caching the `/users/{id}` profile read is safe; caching the authorization lookup would have silently undone a security decision.
- **`SCAN`, never `KEYS`**, for pattern invalidation — `KEYS` is O(N) over the whole keyspace and blocks Redis's single command thread, which on a production instance is a self-inflicted outage. `CacheService.scan_keys` also bounds the worst case at 5000 keys.
- **Token bucket over sliding window**, executed as one atomic Lua script. Fixed window allows 2x the intended rate across a boundary (catastrophic on a login endpoint); sliding-window log is memory-linear in request rate; sliding-window counter can't express burst separately from sustained rate. Token bucket is O(1) in memory and time, makes burst (`capacity`) and sustained rate (`capacity/window`) separate tunables, and yields an **exact** `Retry-After` from the token deficit. `now_ms` is passed *into* the script rather than read via `redis.call("TIME")` to keep it deterministic and testable.
- **Rate limiting is a route dependency, not middleware.** Middleware sees only a method and a path string, so classification means a path-pattern table that rots on the next rename. `/folders` and `/metadata` apply the budget at **router** level so a newly-added route cannot silently be unprotected; `/metadata/search` stacks a tighter budget on top. Per-**chunk** `PUT /uploads/{id}/chunks/{n}` is deliberately NOT limited — one large upload legitimately issues thousands of parallel chunk PUTs (the whole point of Phase 6), so a budget there would throttle correct behavior rather than abuse.
- **Fail-open by default on rate limiting** (`RATE_LIMIT_FAIL_OPEN=true`), loudly logged at ERROR and configurable. This is abuse mitigation sitting behind GCLB, not an authorization control; failing closed would turn a Redis blip into a fleet-wide 429 storm for users mid-upload. The fail-closed path is implemented and tested, not merely described.
- **Exactly one new exception handler.** `RateLimitExceeded` needs a 429 with `Retry-After` that no existing handler can produce; every other new Phase 7 exception subclasses an already-registered base and gets correct HTTP mapping for free via FastAPI's MRO walk — the same technique Phase 6 established. `LockAcquisitionTimeout` subclasses Phase 4's `LockAcquisitionException` specifically so the existing 409 handler still applies unchanged.
- **`DistributedLockService` is a facade, NOT a second lock implementation.** Phase 4's `SET NX PX` + Lua-checked release was already correct. What was missing was centralized timeout policy, ownership introspection (`owns()` vs `is_held`), strict release, and — most importantly — refusing to conflate *contention* (409) with *Redis unreachable* (`DistributedLockError`; **never** treated as "the lock is free", which would defeat the entire point of holding one).
- **Search caching is the most conservative of any entity**: shortest TTL (90s), a hard row-count ceiling (`CACHE_SEARCH_MAX_ITEMS`), a byte ceiling, and coarse per-user `SCAN`-and-delete invalidation on every file write. A result set is a derived view that cannot be invalidated precisely without a reverse index from row to query — a search-engine feature, not a cache feature.
- **Backward-compatibility technique used throughout the service layer**: caching was added via *keyword-only* `cache=`/`invalidator=` parameters defaulting to `None`, and *new* `*_cached` read methods alongside the originals rather than replacing them. Every pre-existing direct construction and every pre-existing call site keeps working; this is why all 145 pre-Phase-7 tests pass unchanged. Routes were touched only to call the `_cached` variant and to attach rate-limit dependencies.
- **Known, acknowledged gaps** (documented, not hidden): (1) invalidation runs inside the request transaction, not post-commit, so a concurrent reader can repopulate with pre-commit data in a sub-millisecond window — bounded by TTL, and narrowed (not eliminated) by `CACHE_WRITE_GUARD_SECONDS`, which as of a Phase 7 follow-up session **ships ON by default at 1.5s** (was off); the airtight fix still needs a SQLAlchemy `after_commit` hook the current per-request Unit of Work doesn't expose to services. (2) No negative caching, so a hot 404 hits Postgres every time (deliberate: caching it would make a just-created resource 404 for a full TTL). (3) No probabilistic early expiration (XFetch). (4) No cache warming and no metrics backend — hit rates are visible only as structured logs, and the cache is cold after every deploy. **Fixed in the same follow-up session** (was previously gap #2 here): descendant breadcrumbs used to go stale on an ancestor rename until TTL; `rename_folder`/`move_folder` now capture the descendant-ID list via the pre-existing `FolderRepository.list_descendants()` before `cascade_rename` mutates paths, and `CacheInvalidator.descendant_breadcrumbs_changed()` deletes each descendant's exact `breadcrumbs` key precisely — no more TTL-bounded staleness for this case.
- **Out of scope this phase, unchanged**: Pub/Sub, background workers, disaster recovery, multi-region, a Prometheus/OpenTelemetry stack, CI/CD, AI features, virus scanning, advanced dedup.

## Phase 8 Design Decisions (see README §15 for full detail, docs/event-driven-architecture.md for the deep dive)

- **The transactional outbox is the whole point, and it required no new transaction-management code.** `OutboxRepository.add_event` is `session.add()` + `flush()`, built from the request-scoped session — and the only `session.commit()` in the entire application is still `app/database/session.py::get_db`. The outbox pattern is exactly the trick of exploiting the Unit of Work that already existed instead of inventing a two-phase commit.
- **What the outbox does NOT buy is exactly-once, and the residual window is deliberate.** If the publisher dies after Pub/Sub accepts a message but before `mark_published` commits, the row stays PENDING and is republished. The inverse ordering (mark published first) would trade a harmless duplicate for a silent loss — strictly worse. The chain is: atomic intent → at-least-once delivery → idempotent consumption.
- **`ProcessedEvent`'s `UniqueConstraint(event_id, consumer_name)` is the guarantee; the `has_processed` pre-check is an optimization only.** Two replicas can both pass the pre-check; only one wins the insert. The loser catches `IntegrityError`, logs a duplicate, and **still ACKs** — a NACK there would request work that is definitionally already done. Same SAVEPOINT technique Phase 6 used for `create_or_get_existing`.
- **Keyed per `(event_id, consumer_name)`, not per `event_id`** — three consumers legitimately process the same event, and an `event_id`-only ledger would let whichever worker arrived first silently block the other two. `consumer_name` is also kept separate from `worker_name` so renaming a Deployment never resets a consumer's idempotency ledger.
- **Derived event IDs are deterministic UUIDv5 over `(parent_event_id, child_event_type)`.** The subtlest bug in the phase: with `uuid4()` every retried fan-out mints fresh identities, downstream dedup never fires, and thumbnails regenerate forever — with no error and no alarm, just cost. There is a test whose only job is to catch that.
- **Work and its ledger row commit together, in one transaction owned by `BaseWorker._handle`.** Same "one commit at the boundary" discipline `get_db` enforces for the API. A consumer that committed its own work separately could crash between the two and re-notify a user.
- **The ack/nack decision lives in exactly ONE place.** If each worker made it for itself they would drift, and an inconsistent ack policy is how events get silently dropped. Every path ends in exactly one `ack()` or one `nack()` — a message that is neither stalls until the deadline expires, which is the worst of both.
- **Non-retryable failures ACK + write `ProcessedEvent(FAILED)`; they are NEVER dead-lettered.** The DLQ is for retry-*exhausted* messages a human might replay after a fix. An unsupported file type will not succeed on attempt 5 either, and filling the DLQ with impossible work is how a DLQ becomes an ignored queue — which looks like coverage while being none.
- **The retryable/non-retryable rule of thumb: is this failure a property of the message, or of the world right now?** Message properties (unparseable envelope, missing payload field, unsupported type, corrupt bytes, deleted file row) are permanent. World properties (GCS timeout, DB unreachable, publish failure) are transient. Getting it wrong in either direction is expensive — one drops real work, the other floods the DLQ.
- **The fan-out (`thumbnail.requested`/`notification.requested`) is published DIRECTLY, bypassing the outbox.** The outbox exists to make a Postgres write atomic with a publish; the file worker performs no business write. With nothing to be atomic *with*, an outbox row would add a table write, a poll interval of latency and a second process's involvement, and buy nothing.
- **Three topics, not one firehose and not one per event type.** Three genuine fan-out boundaries. `notification-events` is separate specifically because it is an **egress** boundary — a wedged third party must never apply backpressure to file processing.
- **No Pub/Sub ordering keys this phase**, decided by auditing the catalog rather than defaulting: every consumer is an idempotent projection with no cross-event sequencing need. Ordering serializes per key, caps throughput at one in-flight message per aggregate, and turns one stuck message into head-of-line blocking. `aggregate_id` is captured on every outbox row anyway, so enabling it later is a publisher change with **no migration**.
- **Ordering that DOES matter is inside the thumbnail worker**: generate and upload the thumbnail first, *then* point the metadata row at it. Reversed, a crash between the two leaves `thumbnail_object_name` referencing a nonexistent object — a dangling pointer served to users. In this order the worst case is an orphaned object the next redelivery overwrites in place.
- **The thumbnail allow-list is checked BEFORE any download and certainly before any decode** — a security property, not an optimization. Pillow must never be handed bytes of an unknown format on the strength of a client-declared MIME type, and `Image.open(formats=...)` pins the decoder so a mislabelled file is rejected rather than sniffed.
- **`PUBSUB_ENABLED` defaults to false, and that is a feature.** The integration lands dark: outbox rows are written transactionally and simply never leave Postgres. Flipping a config value — not deploying code — turns it on, and flipping it back stops all event traffic while events accumulate durably. Same operational pattern as Phase 7's `CACHE_ENABLED`.
- **`EventPublisher` wraps the sync client in `run_in_executor` + `asyncio.wrap_future`** — the same executor-wrapping Phase 3 established for the sync GCS client. The fake returns a real `concurrent.futures.Future` precisely so that skipping either hop fails a test instead of silently blocking the event loop.
- **Backward compatibility was achieved the same way Phase 7 did it**: a keyword-only `outbox: OutboxRepository | None = None` on the four emitting services, defaulting to `None` (= emit nothing). Every pre-existing call site, construction and test kept working untouched — which is why Phase 8 required **zero edits to any pre-existing test**.
- **Correlation/causation come from `structlog.contextvars`, not from a parameter threaded through every method signature.** `RequestContextMiddleware` already binds it, contextvars survive `await`, and `move_file(...)` does not grow an argument every caller must pass.
- **Each worker is the same image with a different `python -m app.workers.<name>`** — one build, one dependency set, one copy of the models that read the same tables. `command:` is exec-form so PID 1 is Python and SIGTERM reaches the graceful drain instead of a shell.
- **Workers get liveness only, as an exec probe on a heartbeat file touched on a TIMER.** No Service selects them, so readiness is a question nobody asks. The heartbeat is independent of message arrival because an idle worker on an empty subscription is healthy, and the probe checks the file's *mtime* so a wedged event loop is caught rather than papered over.
- **Default Kubernetes RollingUpdate for workers**, not the API's `maxUnavailable:0/maxSurge:1`. That tuning keeps HTTP traffic flowing through a deploy; there is no traffic here, and a worker killed mid-message just does not ack.
- **One shared `nimbusfs-config` ConfigMap, extended — no separate worker ConfigMap.** Topic names are shared vocabulary, and a producer and consumer disagreeing about a topic name is a silent total delivery failure; two ConfigMaps is exactly how that disagreement happens. Genuinely per-worker values (the thumbnail worker's `WORKER_CONCURRENCY=3`) are a small per-Deployment `env:` block, which takes precedence over `envFrom`.
- **Four service accounts, one per worker, with scoped GSAs — not one shared "workers" identity.** A compromised notification worker cannot read one user's file, because it has no GCS role at all; it is also the component that will one day talk to a third party, so it is the most exposed. The Kubernetes RBAC Role is deliberately *shared* (it grants nothing interesting); the permissions that genuinely differ are GCP IAM.
- **Known, acknowledged gaps** (documented, not hidden): **(1) NOTHING IN PHASE 8 WAS EVER RUN AGAINST REAL INFRASTRUCTURE, in either session** — no Pub/Sub emulator, no real Pub/Sub, no Postgres, no Redis, no Docker (Docker Desktop was never started), no GKE cluster. Migration `0005` has never been applied to a real database. Every claim rests on design reasoning plus a suite running against in-memory SQLite and hand-written fakes. `docker-compose.yml`'s emulator + 4 worker services and `k8s/16-21` are written and internally consistent, and **neither has been started nor applied**. (2) No HPA or backlog-based autoscaling for any worker — `num_undelivered_messages` is the natural signal and the natural next step. (3) No real email provider: `LoggingNotificationSender` writes a row and logs "would send email (stub)"; no SMTP, no SendGrid/SES/FCM, no template engine, no delivery retry. (4) No DLQ replay tooling — the runbook documents the `gcloud` steps, there is no script, and nothing cross-region. (5) Thumbnails cover exactly 4 raster MIME types (jpeg/png/webp/gif); no PDF, SVG, video keyframe, HEIC or TIFF. (6) No outbox retention/archival — the table only grows, PUBLISHED rows are never pruned. That is a real operational gap, not a hypothetical one. (7) No Pillow `MAX_IMAGE_PIXELS`/pre-decode dimension guard, so a decode bomb is bounded only by the container memory limit and `MAX_DELIVERY_ATTEMPTS`. (8) No metrics backend — every event is structured-logged with metrics-ready fields, nothing scrapes them. (9) Phase 6's stuck-`COMPLETING` reconciliation job is now *possible* to build, and was not built.

## Phase 9 Design Decisions (see README §16 for full detail, docs/high-availability.md + docs/disaster-recovery.md + docs/failure-testing.md + docs/backup-restore.md for the deep dive)

- **HA and DR are evaluated as two separate guarantees, never conflated.** High availability answers "does the system survive a normal infrastructure failure without a human"; disaster recovery answers "can a human bring the system back after a failure too large for that." A system can excel at one and fail the other, so this phase's four docs and this section keep them in separate subsections throughout rather than one blended "reliability" narrative.
- **99.9% availability target, not 99.95%/99.99%**, because the dominant ceiling is Cloud SQL regional HA's recurring failover time, and this codebase has no read/write DB separation to route around it (still documented-not-wired since Phase 4). Claiming a tighter number on top of that would be a number the architecture cannot back — see the Critical Rule this whole phase is built under.
- **`topologySpreadConstraints` added ALONGSIDE Phase 5's `podAntiAffinity`, never replacing it.** They solve different problems: anti-affinity is a relative preference that can clump once "different enough" is satisfied; spread constraints are an absolute `maxSkew` bound. Both stay soft (`preferred...`/`ScheduleAnyway`) for the identical reason Phase 5 chose soft over hard — a degraded zone must never leave a Pod permanently `Pending`.
- **Exactly two workers bumped from 1->2 replicas (outbox-publisher, notification-worker), not a blanket policy.** file-worker/thumbnail-worker were already at 2 since Phase 8. Each bumped Deployment's own YAML header carries the specific reasoning, and explicitly does NOT revisit Phase 8's throughput-based "1 is correct" reasoning — the bump is entirely about zone-redundancy, a different axis.
- **Reconciliation ships read-only and single-direction.** Detects `METADATA_WITHOUT_OBJECT` (the dangerous direction — a user hits 404 on a file that looks fine in their listing) but not `OBJECT_WITHOUT_METADATA` (orphaned GCS objects — costs money, not correctness, and needs a full bucket listing this codebase has never had reason to do). The service has literally no delete/update statement anywhere in its call graph — not a flag gating one, an absence of one — because the user's own instruction was explicit: never auto-delete during reconciliation without real safeguards, and the safest safeguard available this phase was not writing the capability at all.
- **A CronJob, not a Deployment, for the reconciliation job** — it has no queue to drain and no reason to stay resident; `python -m app.workers.reconciliation_job` runs once, exits 0/1/2 (clean/issues-found/scan-incomplete), and that exit code is the whole alerting contract.
- **A fifth, read-only KSA/GSA for reconciliation** (`nimbusfs-reconciliation-ksa`), not a reuse of any existing worker's identity — `roles/storage.objectViewer` + `roles/cloudsql.client` only, matching the "no write code path" property above at the IAM layer too. A future apply/remediation mode would need to widen this grant deliberately, in lockstep with the code gaining the ability to act.
- **Active-passive warm standby chosen for multi-region DR, explicitly rejecting active-active.** No requirement here justifies solving multi-writer Postgres consistency, and doing so "because it sounds enterprise-grade" is precisely what the user's own prompt warned against. Cold standby was rejected the other direction — too slow to meet the chosen <4h RTO once you count provisioning from scratch.
- **GCS durability strategy: keep the regional bucket, recommend a scheduled cross-region object-replication job — NOT a dual-region or multi-region bucket.** Dual-region adds real write-latency cost for a workload with no global-read-locality need; the recommendation is cost-aware, not reflexively the most durable/expensive option, per the user's explicit instruction.
- **A single global external ALB frontend for eventual regional failover, not per-region DNS records.** Removes DNS TTL/propagation delay from the RTO critical path entirely — a strictly better trade for this design than the added complexity would be worth.
- **Nothing in this document is asserted as MEASURED.** Every RTO/RPO/availability number is a justified target with its derivation shown; the Critical Rule this phase was built under bars claiming compliance from configuration existing alone. `docs/failure-testing.md` and `docs/backup-restore.md` exist specifically to give a future session with real GCP access the exact procedure to convert each target into a measurement.
- **Known, acknowledged gaps** (documented, not hidden): **(1) NOTHING IN PHASE 9 WAS EVER RUN AGAINST REAL INFRASTRUCTURE** — no real GKE cluster, Cloud SQL instance, or Memorystore instance was available in this session, same constraint as every prior phase's own infra work. (2) Orphaned-GCS-object detection (the other reconciliation direction) is not implemented. (3) No automatic remediation of a reconciliation finding — a human decides every time. (4) No cross-region object-replication job actually built — recommended and documented only. (5) No GCS Object Versioning/lifecycle/soft-delete actually enabled — no real bucket existed to enable it on. (6) No Terraform — the `gcloud`/`kubectl` commands throughout the four new docs are the IaC-equivalent documentation, not code. (7) The DR runbook (`docs/disaster-recovery.md` §9) has never been executed end to end. (8) No backup-restore drill has ever been run — `docs/backup-restore.md` §3's template is deliberately blank. (9) Phase 6's stuck-`COMPLETING` upload-session reconciliation and backlog-based worker autoscaling both remain undone, as before — Phase 9's reconciliation job is a different mechanism solving a different drift problem, not a solution to either.

## Phase 5 Verification Caveat

No real GKE cluster (or `kind`/`minikube`) was available in this session/environment (`docker ps` failed — Docker Desktop wasn't running), so **the `k8s/` manifests were validated syntactically only**: `python -c "import yaml..."` confirmed all 16 files parse as valid YAML (`k8s/11-networkpolicy.yaml` and `k8s/04-rbac.yaml` are correctly multi-document), and `bash -n` confirmed all 3 `scripts/k8s-*.sh` files are syntactically valid shell. **Nothing was applied to a live cluster; no manifest has been confirmed to actually reconcile successfully against the real Kubernetes API** (e.g. whether every CRD field name/apiVersion is accepted by GKE's actual admission controllers, whether the Pod Security "restricted" profile accepts the Deployment's securityContext as written) — treat `k8s/` as a strong, carefully-reasoned first draft that still needs a real `kubectl apply --dry-run=server` (or a real deploy per `k8s/README.md`) before being trusted in production. If a future session has cluster access, running `./scripts/k8s-deploy.sh` and `./scripts/k8s-smoke-test.sh --full` end-to-end is the natural next verification step.

## Config (`.env.example`)

All Phase 1/2 vars (see README §18) plus Phase 3: `GCS_PROJECT_ID`, `GCS_BUCKET_NAME`, `GCS_CREDENTIALS_PATH` (leave unset outside local dev — ADC/Workload Identity is used instead), `SIGNED_URL_EXPIRATION_MINUTES`, `MAX_UPLOAD_SIZE_MB`, `ALLOWED_MIME_TYPES`, `BLOCKED_EXTENSIONS`. Plus Phase 4: `INSTANCE_ID`/`HOSTNAME` (leave commented out — generated/defaulted per process), `BUILD_VERSION`, `GIT_COMMIT`, `TRUSTED_PROXIES`, `IDEMPOTENCY_KEY_TTL_SECONDS`, `IDEMPOTENCY_LOCK_TIMEOUT_SECONDS`, `LOCK_DEFAULT_TTL_SECONDS`, `LOCK_ACQUIRE_TIMEOUT_SECONDS`, `LOCK_RETRY_INTERVAL_SECONDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `FAIL_FAST_ON_STARTUP`, `SHUTDOWN_GRACE_PERIOD_SECONDS`. Plus Phase 6: `CHUNK_MIN_SIZE_BYTES`, `CHUNK_MAX_SIZE_BYTES`, `CHUNK_DEFAULT_SIZE_BYTES`, `MAX_CHUNKS_PER_UPLOAD`, `MAX_CHUNKED_UPLOAD_SIZE_GB`, `UPLOAD_SESSION_EXPIRATION_MINUTES`. Plus Phase 7: `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS`/`REDIS_SOCKET_TIMEOUT_SECONDS`/`REDIS_RETRY_ON_TIMEOUT`/`REDIS_HEALTH_CHECK_INTERVAL_SECONDS`, `CACHE_ENABLED`, `CACHE_KEY_PREFIX`, `CACHE_TTL_{USER,FOLDER,FOLDER_CHILDREN,FOLDER_BREADCRUMBS,FILE,FILE_VERSIONS,SEARCH}_SECONDS`, `CACHE_MAX_VALUE_BYTES`, `CACHE_SEARCH_MAX_ITEMS`, `CACHE_STAMPEDE_{PROTECTION_ENABLED,LOCK_TTL_SECONDS,WAIT_SECONDS,POLL_INTERVAL_SECONDS}`, `CACHE_WRITE_GUARD_SECONDS`, `RATE_LIMIT_ENABLED`, `RATE_LIMIT_FAIL_OPEN`, and `RATE_LIMIT_<CATEGORY>_REQUESTS`/`_WINDOW_SECONDS` for login/register/metadata/search/upload_initiate/upload_complete/default. **All of these are also mirrored into `k8s/05-configmap.yaml`** (additive keys only, consumed by the existing `envFrom` — no Deployment change was needed). Plus Phase 8: `GCP_PROJECT_ID`, `PUBSUB_ENABLED` (default **false**), `PUBSUB_EMULATOR_HOST`, `FILE_EVENTS_TOPIC`/`UPLOAD_EVENTS_TOPIC`/`NOTIFICATION_EVENTS_TOPIC`, `FILE_WORKER_SUBSCRIPTION`/`THUMBNAIL_WORKER_SUBSCRIPTION`/`NOTIFICATION_WORKER_SUBSCRIPTION`, `MAX_DELIVERY_ATTEMPTS`, `PUBSUB_ACK_DEADLINE`, `OUTBOX_BATCH_SIZE`, `OUTBOX_POLL_INTERVAL`, `WORKER_CONCURRENCY`, `WORKER_HEARTBEAT_INTERVAL_SECONDS`, `WORKER_HEARTBEAT_FILE_PATH`, `WORKER_SHUTDOWN_GRACE_SECONDS`, `THUMBNAIL_MAX_DIMENSION_PX`, `THUMBNAIL_SUPPORTED_CONTENT_TYPES`, `THUMBNAIL_OBJECT_PREFIX`, `OUTBOX_RETRY_BASE_DELAY_SECONDS`/`OUTBOX_RETRY_MAX_DELAY_SECONDS` — **all now mirrored into `k8s/05-configmap.yaml` too** (one shared ConfigMap for the API and all four workers; per-worker overrides live in each Deployment's small `env:` block, which takes precedence over `envFrom`). Plus Phase 9: `RECONCILIATION_ENABLED`, `RECONCILIATION_DRY_RUN`, `RECONCILIATION_BATCH_SIZE`, `RECONCILIATION_MAX_ISSUES` — also mirrored into `k8s/05-configmap.yaml`, consumed only by the new `nimbusfs-reconciliation` CronJob but harmless no-ops for every other workload reading the same ConfigMap.

## Tests

416/416 passing (246 Phases 1-7 + 164 Phase 8 + 6 Phase 9). Run with `pytest -v`. Phase 9's 6 new tests (`tests/test_reconciliation.py`) needed no new conftest wiring at all — they build their own throwaway SQLite engine and `FakeGCSClient` directly, the same pattern `tests/test_thumbnail_worker.py` already established, rather than going through the shared `client`/`db_session` fixtures. Phase 8's tests need no new conftest wiring beyond the one `fake_pubsub_client` override — same DI-driven ease Phase 7 noted for itself. `tests/fakes/fake_pubsub.py` really *stores* messages per topic, so a test asserts "this exact envelope landed on this exact topic," not "publish was called once"; its `publish()` returns a `concurrent.futures.Future` (not a coroutine) precisely because that is what the real client returns and what `EventPublisher` must bridge. It deliberately does NOT simulate Pub/Sub's *server* behavior (ack deadlines, automatic redelivery, DLQ routing) — those are Google's semantics, and faking them would be asserting our guesses about them; where redelivery matters, a test hands the same message to the worker twice explicitly, which is a stronger assertion than a probabilistic one. `tests/test_events_integration.py` is the only file that drives the whole chain across components; it uses a documented shared-session factory so the workers can see the HTTP request transaction's rows (cross-transaction atomicity is tested in `test_event_emission.py` instead, where it belongs). Phase 3 tests never touch real GCS — `tests/fakes/fake_gcs.py::FakeGCSClient` is wired in via `app.dependency_overrides[get_gcs_client]` in `conftest.py`'s `client` fixture. Phase 4 tests never touch real Redis the same way, via `tests/fakes/fake_redis.py::FakeRedisClient` + `app.dependency_overrides[get_redis]`. Phase 6 reuses both fakes (plus `FakeBlob.compose()`, added that phase). **Phase 7 needed no new conftest wiring at all**: `CacheService` and `RateLimiter` are both built from the same `get_redis` dependency, so the single existing override covers them — that was a deliberate DI choice, not luck. Phase 7's 101 new tests live in `tests/test_caching.py` (72) and `tests/test_rate_limiting.py` (29); `FakeRedisClient` gained a controllable clock and a failure-injection mode so TTL/lock-expiry/refill are deterministic and "Redis is down" is a real assertion rather than a mock. **Exception**: `/health` and `/ready` intentionally call the real `check_database_connection()`/`check_redis_connection()` (module-level engine/pool, not request-scoped overrides) so they report actual replica connectivity — their tests assert response *shape* only, not a specific healthy/unhealthy outcome, and `conftest.py` pins `RETRY_*` env vars low before `app.main` is imported so those tests don't pay multi-second real-backoff costs against an unreachable Postgres/Redis in a sandboxed run. Phase 6 adds one more such test-speed override: `CHUNK_MIN_SIZE_BYTES=1024` (production default is 1 MiB — full-size chunks in every test would be needlessly slow).

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

**Phase 9 is complete.** Resume roadmap work at **Phase 10** per README §24 "Future Roadmap" — the user will provide that prompt in a future session. Do not regenerate Phases 1–9; extend the existing codebase only.

The single highest-value thing a future session could do *before* new feature work, if it has real infrastructure available, is the same one Phase 8 named and Phase 9 did not resolve either: **verify everything against real infrastructure.** Specifically, in rough priority order: (1) `alembic upgrade head` against a real Postgres (still never done, migrations `0004`/`0005` untested against real Postgres since they were written), (2) stand up a real regional GKE cluster and run the chaos scenarios in `docs/failure-testing.md` §2, (3) provision a real Cloud SQL Regional-HA instance and Memorystore Standard-tier instance and run `docs/backup-restore.md` §3's restore drill plus a real `gcloud sql instances failover`, (4) if a second GCP project/region is available, execute `docs/disaster-recovery.md` §9's failover runbook end to end and fill in its RTO/RPO measurement template. None of that has ever been done in any session across all 9 phases, and it remains the one gap that makes every HA/DR/event-driven/Kubernetes claim in this project provisional rather than merely incomplete.

The single highest-value thing a future session could do *before* new feature work, if it has real infrastructure available, is **verify Phase 8 against it**: `alembic upgrade head`/`downgrade -1`/`upgrade head` against a real Postgres, then `docker compose up` with the Pub/Sub emulator and the four workers, then `kubectl apply --dry-run=server` on `k8s/16-21`. None of that has ever been done, and it is the one gap that makes every other Phase 8 claim provisional rather than merely incomplete.

Other things to keep in mind before/while building further:
- The Phase 5 GKE-deployment verification caveat is still open (no manifest applied to a real cluster yet) and unrelated placeholder values (`<PROJECT_ID>`, domain, image tag, `11-networkpolicy.yaml`'s Cloud SQL/Memorystore CIDRs) still need replacing before a real deploy — see `k8s/README.md`. Phase 7 makes the Memorystore CIDR placeholder *more* consequential than before: Redis is now on the read path of every cached request, so a wrong egress rule degrades every read instead of just breaking health checks.
- Phase 6's known, deliberate gaps: no automatic reconciliation of an upload session stuck mid-`COMPLETING` after a process crash (needs a future background-worker phase — Phase 8's workers may finally be the right place to add this, once Phase 8 itself is finished); no content-dedup extension to the chunked-upload path; the k6/Locust load tests were written but **not actually run** in this session (no load-testing infrastructure available) — see `scripts/load-test/README.md`.
- Phase 7's known, deliberate gaps (all listed in "Phase 7 Design Decisions" above and in `docs/PHASE_7_REDIS_DESIGN.md`): invalidation is not post-commit (narrowed by the now-default-on `CACHE_WRITE_GUARD_SECONDS`); no negative caching; no probabilistic early expiration; no cache warming; no metrics backend (structured logs only). Descendant-breadcrumb staleness on ancestor rename was fixed in a same-day follow-up session (precise invalidation via `list_descendants` + `descendant_breadcrumbs_changed`), so it's no longer on this list. `scripts/benchmark/benchmark_cache.py` was written but, like the Phase 6 load tests, **not run** — no real Postgres/Redis was available in the original Phase 7 session.
- Phase 8's honest gaps (full list in "Phase 8 Design Decisions" above, and README §15.16): **no part of Phase 8 has ever been run against real infrastructure in any session** — migration `0005` has never touched a real Postgres, the Pub/Sub emulator was never started, Docker Desktop was never started, and `k8s/16-21` were never applied. Plus: no backlog autoscaling, no real email provider, no DLQ replay tooling, no outbox retention/archival job (the table only grows), thumbnails limited to 4 raster MIME types, no Pillow pixel-count guard, no metrics backend.
- Phase 9's honest gaps (full list in "Phase 9 Design Decisions" above, and README §16.15/§16.18): **no HA/DR claim in Phase 9 has ever been MEASURED against real infrastructure** — no real regional GKE cluster, Cloud SQL Regional-HA instance, or Memorystore Standard-tier instance existed in this session. Plus: orphaned-GCS-object reconciliation not implemented (only the other, more dangerous direction is), no automatic remediation of a reconciliation finding, no cross-region object-replication job actually built (recommended only), no GCS Object Versioning/lifecycle/soft-delete actually enabled on a real bucket, no Terraform, the DR failover runbook has never been executed, and the backup-restore drill template (`docs/backup-restore.md` §3) is deliberately blank.
- Phase 4 left read replicas, row-level optimistic locking, and OpenTelemetry as designed-but-not-wired; Phase 5 left CI/CD and a monitoring stack as documented-but-not-built. (Real rate limiting and Redis metadata caching were on that list until Phase 7 shipped them; multi-zone HA and disaster-recovery design until Phase 9 did — though Phase 9's own MEASURED-vs-DESIGNED gap above still applies.) Revisit any of these only if a future phase's prompt actually calls for them — don't retrofit speculatively.
