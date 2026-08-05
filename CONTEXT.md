# NimbusFS — Project Context

Purpose of this file: give a fresh AI session (or human) full context on this project in one read, without needing to re-explore the codebase from scratch. Written 2026-08-04; updated 2026-08-05 after completing Phase 4.

## Current Status: Phases 1–4 complete, repo healthy

The repo previously had **committed, unresolved Git merge-conflict markers** in 8 files (from a bad merge, `086377c "Merged existing repository"`) plus a parallel orphaned legacy implementation tree. **All of that has been resolved** — see "History: What Was Fixed" below for the record. As of now:

- `app.main` imports cleanly, the app starts, all routes are live.
- Full test suite: **104/104 passing** (57 Phase 1/2 + 19 Phase 3 + 28 Phase 4), against in-memory SQLite (`aiosqlite`) — no external services needed to run `pytest`. `/health`/`/ready` deliberately check *real* DB/Redis connectivity (see `app/database/session.py`/`redis.py`), so those two routes' test assertions are shape-only, not "must be healthy" — see `tests/conftest.py` for how the suite still stays fast without real infra.
- No known unresolved conflicts, no orphaned legacy code, no stray env files.

## What NimbusFS Is

A **cloud-native distributed file storage platform** (Google-Drive-style) built with Python, FastAPI, PostgreSQL, and Google Cloud Storage. Built in phases (~15-phase roadmap). Currently implemented:

- **Phase 1**: user registration/auth (JWT access+refresh, role-based access)
- **Phase 2**: folder hierarchy, file metadata, soft-delete/trash, versioning, search & pagination
- **Phase 3**: real file upload/download via Google Cloud Storage, signed URLs, streaming downloads with Range support, SHA-256 content-based duplicate detection, upload/metadata rollback consistency
- **Phase 4**: distributed backend architecture — stateless multi-replica design, correlation/trace/server-ID propagation + structured logging, `/health`+`/ready`+`/live` endpoints, fail-fast startup + graceful shutdown lifecycle, Redis-backed distributed locks, Redis-backed `Idempotency-Key` support on `POST /files/upload`, DB/Redis/Storage retry-with-backoff, a circuit breaker primitive, trusted-proxy/forwarded-header handling, a rate-limit middleware placeholder. No Kubernetes/deployment manifests yet — that's Phase 5.

**Not yet built** (future phases, per README §20): Kubernetes/GKE deployment + autoscaling, chunked/resumable uploads, sharing/permissions between users, virus scanning (placeholder only), thumbnails, full-text content search, Pub/Sub background workers, real rate limiting, Redis *metadata* caching (Phase 4 only built the plumbing), CI/CD, Terraform, observability/OpenTelemetry tracing.

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
| Containers | `Dockerfile` (python:3.12-slim) + `docker-compose.yml` (postgres, redis, app) |

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
  core/
    config/settings.py         Settings, get_settings() — includes GCS_*, MAX_UPLOAD_SIZE_MB, ALLOWED_MIME_TYPES,
                                BLOCKED_EXTENSIONS, and Phase 4: INSTANCE_ID/HOSTNAME/BUILD_VERSION/GIT_COMMIT,
                                TRUSTED_PROXIES, IDEMPOTENCY_*, LOCK_*, RETRY_*, FAIL_FAST_ON_STARTUP
    security/password.py       hashing (bcrypt)
    security/tokens.py         JWT encode/decode, TokenType, decode_token()
    enums.py                   UserRole, FileStatus, etc.
    server_info.py             Phase 4: get_server_identity() — instance_id/hostname/pid/version/build singleton
    retry.py                   Phase 4: retry_async() — exponential backoff + full jitter
    circuit_breaker.py         Phase 4: CircuitBreaker — closed/open/half-open primitive, in-process per instance
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
                                 GCSClientDep, and Phase 4: DistributedLockFactoryDep/IdempotencyServiceDep
  models/                       user.py, refresh_token.py, folder.py, file_metadata.py (+Phase 3 storage columns), file_version.py, mixins.py
  repositories/                 base.py + one repo per entity; file_metadata_repository.py has get_by_checksum/object_name_in_use for dedup
  services/                     auth_service.py, user_service.py, folder_service.py, metadata_service.py,
                                 search_service.py, trash_service.py, version_service.py,
                                 storage_service.py (Phase 3, GCS wrapper — ONLY module importing google.cloud.storage),
                                 file_validation_service.py (Phase 3), file_upload_service.py (Phase 3 orchestrator),
                                 idempotency_service.py (Phase 4: Redis-backed Idempotency-Key contract)
  schemas/                      auth.py, user.py, folder.py, file_metadata.py (+FileUploadResponse/SignedUrlResponse),
                                 health.py (Phase 4: ServerInfo/ReadinessResponse/LivenessResponse, HealthCheckResponse
                                 restructured — version/environment now nested under `server`),
                                 pagination.py, response.py (APIResponse[T] envelope), search.py, sorting.py
  exceptions/                   custom_exceptions.py (+Storage* exceptions; Phase 4: LockAcquisitionException,
                                 CircuitBreakerOpenException, ServiceUnavailableException,
                                 IdempotencyKeyReplayedException, IdempotencyKeyInProgressException),
                                 handlers.py (matching handlers for all of the above)
  logging/logger.py             structlog config, get_logger()
  middleware/                   request_context.py (Phase 4: adds correlation_id/trace_id/server_id, more response
                                 headers), security_headers.py, proxy_headers.py (Phase 4: TrustedProxyMiddleware),
                                 rate_limit.py (Phase 4: RateLimitPlaceholderMiddleware — explicit no-op)
  utils/                        path_utils.py (materialized-path helpers), response.py
alembic/versions/               0001_initial, 0002_metadata, 0003_storage (adds GCS columns to file_metadata)
                                 — no new migration in Phase 4 (no model/schema changes)
tests/
  conftest.py                  client/db fixtures + fake_gcs_client/fake_redis_client fixtures (override
                                get_gcs_client/get_redis for every test); Phase 4: pins RETRY_* env vars low
                                before app import so /health-touching tests stay fast
  fakes/fake_gcs.py             FakeGCSClient/FakeBucket/FakeBlob — in-memory GCS stand-in, no real network calls
  fakes/fake_redis.py           Phase 4: FakeRedisClient — in-memory stand-in for the redis.asyncio surface
                                 NimbusFS actually uses (set/get/delete/eval/ping), no real Redis needed
  test_health/registration/login/protected_routes/folders/metadata/search.py   Phase 1/2 tests
  test_file_storage.py          Phase 3 tests (upload/download/range/signed-url/replace/permanent-delete/dedup/rollback/failure)
  test_distributed.py           Phase 4 tests (idempotency, distributed locks, retry, circuit breaker, correlation
                                 IDs, graceful degradation, concurrency) — see README §19 for the full list
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

## Data Model highlights

- **FileMetadata** (`file_metadata`) — Phase 2 columns (id, owner_id, folder_id, original_filename, stored_filename [unique per-row reservation], extension, mime_type, size, checksum, version, status) **plus Phase 3 storage columns**: `storage_provider`, `bucket_name`, `object_name` (indexed, **deliberately NOT unique** — content-dedup lets multiple rows share one object), `public_url` (always NULL — bucket is private), `storage_class`, `etag`, `upload_status` (pending/completed/failed), `uploaded_at`.
- `AuditMixin.updated_at` uses a **Python-side** `onupdate=lambda: datetime.now(timezone.utc)`, not a server-side `func.now()` — this was a real bug fix (see History below); don't revert it to a server-side onupdate, it will reintroduce an async `MissingGreenlet` crash on any mutate-then-serialize request.

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

## Config (`.env.example`)

All Phase 1/2 vars (see README §13) plus Phase 3: `GCS_PROJECT_ID`, `GCS_BUCKET_NAME`, `GCS_CREDENTIALS_PATH` (leave unset outside local dev — ADC/Workload Identity is used instead), `SIGNED_URL_EXPIRATION_MINUTES`, `MAX_UPLOAD_SIZE_MB`, `ALLOWED_MIME_TYPES`, `BLOCKED_EXTENSIONS`. Plus Phase 4: `INSTANCE_ID`/`HOSTNAME` (leave commented out — generated/defaulted per process), `BUILD_VERSION`, `GIT_COMMIT`, `TRUSTED_PROXIES`, `IDEMPOTENCY_KEY_TTL_SECONDS`, `IDEMPOTENCY_LOCK_TIMEOUT_SECONDS`, `LOCK_DEFAULT_TTL_SECONDS`, `LOCK_ACQUIRE_TIMEOUT_SECONDS`, `LOCK_RETRY_INTERVAL_SECONDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `FAIL_FAST_ON_STARTUP`, `SHUTDOWN_GRACE_PERIOD_SECONDS`.

## Tests

104/104 passing. Run with `pytest -v`. Phase 3 tests never touch real GCS — `tests/fakes/fake_gcs.py::FakeGCSClient` is wired in via `app.dependency_overrides[get_gcs_client]` in `conftest.py`'s `client` fixture. Phase 4 tests never touch real Redis the same way, via `tests/fakes/fake_redis.py::FakeRedisClient` + `app.dependency_overrides[get_redis]`. **Exception**: `/health` and `/ready` intentionally call the real `check_database_connection()`/`check_redis_connection()` (module-level engine/pool, not request-scoped overrides) so they report actual replica connectivity — their tests assert response *shape* only, not a specific healthy/unhealthy outcome, and `conftest.py` pins `RETRY_*` env vars low before `app.main` is imported so those tests don't pay multi-second real-backoff costs against an unreachable Postgres/Redis in a sandboxed run.

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

Resume roadmap work at **Phase 5** (Kubernetes/GKE deployment — manifests, autoscaling, ingress; see README §20) — the user plans to provide the Phase 5 prompt in a future session. Do not regenerate Phases 1–4; extend the existing codebase only. Phase 4 deliberately left several things designed-but-not-wired (read replicas, row-level optimistic locking, real rate limiting, Redis metadata caching, OpenTelemetry) — revisit only if a future phase's prompt actually calls for them, don't retrofit speculatively.
