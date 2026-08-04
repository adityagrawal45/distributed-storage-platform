# NimbusFS — Project Context

Purpose of this file: give a fresh AI session (or human) full context on this project in one read, without needing to re-explore the codebase from scratch. Written 2026-08-04, updated same day after completing Phase 4.

## Current Status: Phases 1–4 complete, repo healthy

The repo previously had **committed, unresolved Git merge-conflict markers** in 8 files (from a bad merge, `086377c "Merged existing repository"`) plus a parallel orphaned legacy implementation tree. **All of that has been resolved** — see "History: What Was Fixed" below for the record. As of now:

- `app.main` imports cleanly, the app starts, all routes are live.
- Full test suite: **124/124 passing** (76 Phase 1–3 + 48 Phase 4), against in-memory SQLite (`aiosqlite`) and `fakeredis` — no external Postgres/Redis/GCS services needed to run `pytest`.
- No known unresolved conflicts, no orphaned legacy code, no stray env files.

## What NimbusFS Is

A **cloud-native distributed file storage platform** (Google-Drive-style) built with Python, FastAPI, PostgreSQL, and Google Cloud Storage. Built in phases (~15-phase roadmap). Currently implemented:

- **Phase 1**: user registration/auth (JWT access+refresh, role-based access)
- **Phase 2**: folder hierarchy, file metadata, soft-delete/trash, versioning, search & pagination
- **Phase 3**: real file upload/download via Google Cloud Storage, signed URLs, streaming downloads with Range support, SHA-256 content-based duplicate detection, upload/metadata rollback consistency
- **Phase 4**: distributed backend architecture — stateless-by-construction request handling, server identity, structured distributed logging, correlation/trace ID propagation, Redis infrastructure (cache + distributed lock, both infra-only — not yet used by business logic), Postgres connection pooling/retry/optimistic-locking, global `Idempotency-Key` support, `/health`+`/ready`+`/live`, fail-fast startup + graceful-drain shutdown. Full detail in README §13. **No Kubernetes yet** — that's Phase 5, and this phase exists specifically to make that transition a non-rewrite.

**Not yet built** (future phases, per README §19): Kubernetes/GKE deployment, chunked/resumable uploads, sharing/permissions between users, virus scanning (placeholder only), thumbnails, full-text content search, Pub/Sub background workers, real metadata caching (Phase 4 built the cache *infrastructure* only), a real rate-limiting policy (Phase 4 built the *placeholder* only), CI/CD, Terraform, observability/metrics.

## Tech Stack

| Concern | Choice |
|---|---|
| Framework | FastAPI 0.115.x + Uvicorn (ASGI) |
| Validation/config | Pydantic v2 + pydantic-settings |
| Database | PostgreSQL 16 (`postgres:16-alpine` in docker-compose) |
| DB driver | `asyncpg` (app), `psycopg2-binary` (Alembic, sync) |
| ORM | SQLAlchemy 2.0, async, `Mapped`/`mapped_column` style |
| Migrations | Alembic 1.14 (`alembic/versions/`) |
| Cache | Redis 7 — health checks, plus Phase 4's cache/lock/idempotency/rate-limit infrastructure |
| Auth | JWT (`python-jose[cryptography]`) + `passlib[bcrypt]`/`bcrypt`; OAuth2 Password flow with access+refresh token rotation |
| Logging | `structlog` (structured JSON; every log line carries request/correlation/trace/server IDs as of Phase 4 — see README §13.15) |
| Cloud Storage | `google-cloud-storage` SDK, private bucket, V4 signed URLs, MIME sniffing via `filetype` |
| Testing | `pytest` + `pytest-asyncio` (`asyncio_mode = auto`), `httpx.AsyncClient`, in-memory SQLite (`aiosqlite`), hand-written `FakeGCSClient` (`tests/fakes/fake_gcs.py`), `fakeredis` (Phase 4) — no external services or real GCS/Redis needed |
| Containers | `docker/Dockerfile` (python:3.12-slim, multi-stage) + `docker-compose.yml` (postgres, redis, **app1/app2/app3 + nginx load balancer**, Phase 4 — see README §13.2/§15) |

## Directory Map (live code)

```
app/
  main.py                    create_application() factory, lifespan (fail-fast startup/graceful-drain shutdown, Phase 4), middleware, exception handlers, mounts api_router
  api/v1/
    router.py                 wires all sub-routers together, mounted at settings.API_V1_PREFIX (/api/v1)
    auth/routes.py             /auth/* endpoints
    users/routes.py            /users/* endpoints
    folders/routes.py          /folders/* endpoints
    metadata/routes.py         /metadata/* endpoints (Phase 2, metadata-only, no GCS awareness)
    files/routes.py            /files/* endpoints (Phase 3: upload/download/signed-url/replace/permanent-delete)
    trash/routes.py            /trash endpoint
    health/routes.py           /health, /ready, /live endpoints (Phase 4: readiness/liveness added, /health enriched)
  core/
    config/settings.py         Settings, get_settings() — includes GCS_*, MAX_UPLOAD_SIZE_MB, ALLOWED_MIME_TYPES, BLOCKED_EXTENSIONS, Phase 4's distributed-backend knobs (see README §13.7)
    security/password.py       hashing (bcrypt)
    security/tokens.py         JWT encode/decode, TokenType, decode_token()
    server_identity.py         Phase 4: SERVER_IDENTITY (instance_id/hostname/pid/version), computed once at import time
    retry.py                   Phase 4: retry_async() — generic exponential backoff + jitter
    circuit_breaker.py         Phase 4: CircuitBreaker — guards Redis calls only (not Postgres/GCS, see README §13.19)
    distributed_lock.py        Phase 4: DistributedLock — Redis SET-NX/WATCH-MULTI lock; not called by any endpoint yet
    enums.py                   UserRole, FileStatus, etc.
  database/
    session.py                  async engine/session, declarative Base; Phase 4: get_db_read/read_engine (infra only), is_retryable_db_error(), retry-wrapped check_database_connection()
    redis.py                    redis pool + check_redis_connection(); Phase 4: returns (healthy, latency_ms), retry-wrapped. ALL Redis consumers must call `redis_db.get_redis_client()` via module attribute, not a bare import — this is the one seam tests monkeypatch to fakeredis (see tests/conftest.py::fake_redis_client)
    gcs.py                      GCS client factory (ADC in prod, key file path in dev) — Phase 3; Phase 4: check_storage_connection()
  dependencies/
    auth.py                     get_current_user, CurrentUser, require_role(); Phase 4: binds user_id into structlog contextvars on successful auth
    providers.py                DI wiring for repositories/services, incl. StorageServiceDep/FileUploadServiceDep/GCSClientDep/CacheServiceDep (Phase 4)
  models/                       user.py, refresh_token.py, folder.py, file_metadata.py (+Phase 3 storage columns +Phase 4 lock_version/version_id_col), file_version.py, mixins.py
  repositories/                 base.py + one repo per entity; file_metadata_repository.py has get_by_checksum/object_name_in_use for dedup
  services/                     auth_service.py, user_service.py, folder_service.py, metadata_service.py,
                                 search_service.py, trash_service.py, version_service.py,
                                 storage_service.py (Phase 3, GCS wrapper — ONLY module importing google.cloud.storage),
                                 file_validation_service.py (Phase 3), file_upload_service.py (Phase 3 orchestrator),
                                 cache_service.py (Phase 4, generic Redis cache — infra only, NOT used for metadata caching yet)
  schemas/                      auth.py, user.py, folder.py, file_metadata.py (+FileUploadResponse/SignedUrlResponse), health.py (Phase 4: ServerInfo/ReadinessCheckResponse/LivenessCheckResponse),
                                 pagination.py, response.py (APIResponse[T] envelope), search.py, sorting.py
  exceptions/                   custom_exceptions.py (+Storage* exceptions +Phase 4: ConcurrentModificationException/LockAcquisitionException/IdempotencyConflictException), handlers.py (+Storage*/FileTooLarge/UnsupportedFileType handlers +Phase 4: StaleDataError/CircuitOpenError/RetryExhaustedError handlers)
  logging/logger.py             structlog config, get_logger()
  middleware/                   request_context.py (Phase 4: rewritten for correlation/trace/server IDs, active_requests counter), security_headers.py, idempotency.py (Phase 4), rate_limit.py (Phase 4, placeholder — off by default)
  utils/                        path_utils.py (materialized-path helpers), response.py (dead/unused legacy file, pre-dates Phase 1 cleanup — not app/schemas/response.py, don't confuse the two), network.py (Phase 4: trusted-proxy-aware get_client_ip/is_forwarded_https)
alembic/versions/               0001_initial, 0002_metadata, 0003_storage (adds GCS columns to file_metadata), 0004_distributed (adds lock_version to file_metadata)
docker/nginx.conf               Phase 4: local Google Cloud Load Balancer stand-in (least_conn across app1/app2/app3)
tests/
  conftest.py                  client/db fixtures + fake_gcs_client fixture + fake_redis_client fixture (Phase 4, monkeypatches app.database.redis.get_redis_client); client fixture now sets app.state.ready=True (lifespan never runs under ASGITransport)
  fakes/fake_gcs.py             FakeGCSClient/FakeBucket/FakeBlob — in-memory GCS stand-in, no real network calls; FakeBucket.exists() added for Phase 4 storage health check
  test_health/registration/login/protected_routes/folders/metadata/search.py   Phase 1/2 tests
  test_file_storage.py          Phase 3 tests (upload/download/range/signed-url/replace/permanent-delete/dedup/rollback/failure)
  test_redis_infrastructure.py  Phase 4: cache/lock/retry/circuit-breaker
  test_idempotency.py           Phase 4: Idempotency-Key replay + duplicate-upload prevention
  test_optimistic_locking.py    Phase 4: concurrent-update StaleDataError (standalone two-session race, not the shared client fixture)
  test_distributed_backend.py   Phase 4: /ready, /live, correlation/trace/server IDs, trusted-proxy IP, startup/shutdown lifecycle
```

## API Surface (all under `/api/v1`)

Every response uses the standard envelope, `app/schemas/response.py::APIResponse[T]`: `{success, message, data, errors, timestamp, request_id}`. Every mutating endpoint additionally accepts an optional `Idempotency-Key` header (Phase 4, global middleware — see README §13.13).

**Health**: `GET /health` (full dependency status), `GET /ready` (load-balancer readiness probe), `GET /live` (liveness probe, checks nothing external)

**Auth** (`/auth`): `POST /register`, `POST /login` (OAuth2 form), `POST /refresh`, `POST /logout`

**Users** (`/users`, Bearer): `GET /me`, `GET /{user_id}` (admin only)

**Folders** (`/folders`, Bearer): full CRUD + tree/breadcrumb/trash/restore/permanent-delete (see README §5)

**File Metadata** (`/metadata`, Bearer, Phase 2 — metadata rows only, no bytes): CRUD, search, rename, move, trash/restore/permanent-delete, versions

**Files** (`/files`, Bearer, Phase 3 — actual bytes in GCS):
- `POST /files/upload` — multipart upload; creates metadata + bytes atomically (rollback on failure)
- `GET /files/{id}/download` — streaming, supports `Range` header (206 partial content)
- `GET /files/{id}/signed-url?expires_in_minutes=` — time-boxed V4 signed URL
- `PUT /files/{id}/replace` — new version, new object, old object cleaned up after swap
- `DELETE /files/{id}/permanent` — deletes GCS object (if unshared) + DB row; requires prior soft-delete via `/metadata/{id}`

**Trash** (`/trash`, Bearer): `GET /trash` — combined `{folders, files}`

## Data Model highlights

- **FileMetadata** (`file_metadata`) — Phase 2 columns (id, owner_id, folder_id, original_filename, stored_filename [unique per-row reservation], extension, mime_type, size, checksum, version, status) **plus Phase 3 storage columns**: `storage_provider`, `bucket_name`, `object_name` (indexed, **deliberately NOT unique** — content-dedup lets multiple rows share one object), `public_url` (always NULL — bucket is private), `storage_class`, `etag`, `upload_status` (pending/completed/failed), `uploaded_at`, **plus Phase 4**: `lock_version` (optimistic-concurrency-control column, wired via `__mapper_args__ = {"version_id_col": lock_version}` — NOT the same field as `version`, which is the file's content revision; a stale write raises `sqlalchemy.orm.exc.StaleDataError`, mapped to HTTP 409 by `app/exceptions/handlers.py::stale_data_exception_handler`).
- `AuditMixin.updated_at` uses a **Python-side** `onupdate=lambda: datetime.now(timezone.utc)`, not a server-side `func.now()` — this was a real bug fix (see History below); don't revert it to a server-side onupdate, it will reintroduce an async `MissingGreenlet` crash on any mutate-then-serialize request.

## Phase 3 Design Decisions (see README §10 for full detail)

- Object naming: `{tenant}/{owner_id}/{year}/{month}/{uuid4}.{ext}` — never the user's filename.
- Duplicate detection: SHA-256 based; identical content reuses the existing `object_name` instead of re-uploading. This is *why* `object_name` has no unique constraint.
- Upload rollback: if metadata persistence fails after a real (non-deduped) GCS upload succeeded, the orphaned object is deleted; if that delete also fails, raises `RollbackFailedException` rather than swallowing it.
- Replace: uploads to a brand-new object, swaps metadata, *then* deletes the old object (only if unreferenced) — never overwrites in place.
- Soft delete (`/metadata/{id}`) never touches GCS bytes (recoverable via restore); permanent delete (`/files/{id}/permanent`) is the only path that removes bytes, and only if no other row still shares the object.
- Buckets are always private; signed URLs are the only sanctioned direct-access path.

## Phase 4 Design Decisions (see README §13.19 for full detail)

- Contextvars (not explicit parameters) carry request/correlation/trace/server/user IDs through every layer — bound once in `RequestContextMiddleware` + `get_current_user`, read nowhere explicitly, present in every log line for free.
- Idempotency is global middleware (`app/middleware/idempotency.py`), not a per-route decorator — every `POST`/`PUT`/`PATCH`/`DELETE` gets `Idempotency-Key` support automatically.
- Cache layer fails OPEN on Redis outage (a miss, never an exception); distributed lock fails CLOSED (mutual exclusion that silently stops being mutual exclusion is worse than an error). Don't "fix" one to match the other — they're intentionally different.
- Circuit breaker (`app/core/circuit_breaker.py`) guards Redis only, never Postgres/GCS — those have no correct degraded mode, so a breaker there would have nothing useful to do when open.
- Optimistic locking (`lock_version`) is scoped to `FileMetadata` only in this phase, not `Folder` — extend the same pattern there later if a concrete race surfaces; don't add it speculatively.
- `app.state.ready` is the single source of truth for `/ready`; it's `False` until every startup dependency check passes AND `False` again from the first instant of shutdown — never flip it early.

## Config (`.env.example`)

All Phase 1/2 vars (see README §12) plus Phase 3: `GCS_PROJECT_ID`, `GCS_BUCKET_NAME`, `GCS_CREDENTIALS_PATH` (leave unset outside local dev — ADC/Workload Identity is used instead), `SIGNED_URL_EXPIRATION_MINUTES`, `MAX_UPLOAD_SIZE_MB`, `ALLOWED_MIME_TYPES`, `BLOCKED_EXTENSIONS`, plus Phase 4: `INSTANCE_ID`, `BUILD_VERSION`, `GIT_COMMIT_SHA`, `DEPENDENCY_RETRY_*`, `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`, `IDEMPOTENCY_*`, `DISTRIBUTED_LOCK_DEFAULT_TTL_SECONDS`, `RATE_LIMIT_*`, `TRUSTED_PROXIES`, `CIRCUIT_BREAKER_*`, `DATABASE_READ_REPLICA_URL` (see README §12/§13.7 for the full annotated list).

## Tests

124/124 passing (76 Phase 1–3 + 48 Phase 4). Run with `pytest -v`. Phase 3 tests never touch real GCS — `tests/fakes/fake_gcs.py::FakeGCSClient` is wired in via `app.dependency_overrides[get_gcs_client]` in `conftest.py`'s `client` fixture, so every test (not just storage ones) gets the fake transparently. Phase 4 tests never touch real Redis the same way, via `fake_redis_client` monkeypatching `app.database.redis.get_redis_client` — **any new Redis-touching code must call `redis_db.get_redis_client()` through the module (not `from app.database.redis import get_redis_client`)** or this monkeypatch silently won't apply to it.

## History: What Was Fixed (2026-08-04 session)

For the record — these are resolved, not open issues:
1. **8 files had committed merge-conflict markers** (`app/main.py`, `app/api/__init__.py`, `app/api/v1/__init__.py`, `app/services/auth_service.py`, `app/services/user_service.py`, `tests/test_health.py`, `requirements.txt`, `alembic.ini`) — resolved in favor of the HEAD/Phase-2 side.
2. **Orphaned legacy tree deleted**: `app/domain/`, `app/infrastructure/`, `app/api/dependencies.py`, flat legacy route files, `app/core/config.py`/`security.py`/`logging.py`/`exceptions.py`, `migrations/`, `tests/test_auth.py`, stray `.env .example` typo file.
3. **`app/repositories/file_metadata_repository.py` was an empty file** — rebuilt from its usage in `metadata_service.py`/`search_service.py`.
4. **`app/schemas/file_metadata.py` contained the wrong content** (a duplicate of `folder.py`) — rebuilt with the correct `FileMetadataCreate`/`Read`/`Update`/etc. matching the model and routes.
5. **Async ORM bug**: `AuditMixin.updated_at`'s server-side `onupdate=func.now()` caused `MissingGreenlet` crashes on any request that mutated then serialized a row mid-request (e.g. folder rename, metadata update). Fixed by switching to a Python-side `onupdate` callable.
6. Also found during Phase 3 test-writing: `object_name` was initially modeled `unique=True`, which broke content-deduplication (two rows can legitimately share one object). Removed the uniqueness constraint, kept a plain index.

## Suggested Next Steps

Resume roadmap work at **Phase 5** (Kubernetes/GKE deployment) per README §19 — the user plans to provide the Phase 5 prompt in a future session. Do not regenerate Phases 1–4; extend the existing codebase only. Phase 5's job is to take the already-stateless, already-multi-instance-ready backend from Phase 4 and actually deploy it to GKE (Deployments/Services/Ingress/HPA) — the app code shouldn't need to change much, if at all, for that transition; if it does, that's a signal Phase 4's statelessness has a gap worth revisiting first.
