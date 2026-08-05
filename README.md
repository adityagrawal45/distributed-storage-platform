| Directory | Responsibility |
|---|---|
| `api/` | HTTP concerns only: routing, request/response wrapping |
| `core/config` | Typed, environment-driven settings |
| `core/security` | Password hashing, JWT issuing/verification |
| `database/` | Engine/session/pool management, health checks |
| `models/` | ORM table definitions |
| `repositories/` | Query/persistence logic per entity |
| `services/` | Business rules, orchestration across repositories |
| `schemas/` | Input validation & output serialization contracts |
| `dependencies/` | FastAPI `Depends` graph (DI container) |
| `middleware/` | Cross-cutting request/response processing |
| `exceptions/` | Domain exceptions + their translation to HTTP |
| `logging/` | Structured logging setup |
| `utils/` | Small stateless helper functions (e.g. path building) |
| `tests/` | Unit/integration tests, fixtures |

## 4. Database Design

**`users`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | `uuid4`, avoids sequential ID leakage |
| first_name / last_name | VARCHAR(100) | required |
| email | VARCHAR(255) | unique, indexed — login identifier |
| hashed_password | VARCHAR(255) | bcrypt hash, never returned by the API |
| role | ENUM(`user`,`admin`) | native Postgres enum |
| is_active | BOOLEAN | default `true`; supports account suspension |
| is_verified | BOOLEAN | default `false`; reserved for future email verification |
| created_at / updated_at | TIMESTAMPTZ | server-side defaults via `func.now()` |

**`refresh_tokens`**

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| jti | UUID | unique, indexed — the token's JWT ID, NOT the raw token |
| user_id | UUID (FK → users.id, `ON DELETE CASCADE`) | indexed |
| revoked | BOOLEAN | default `false`; flips on rotation/logout |
| expires_at | TIMESTAMPTZ | mirrors the JWT's own `exp` claim |
| created_at | TIMESTAMPTZ | |

We deliberately never persist the raw refresh token — only its `jti` — so a
database leak cannot be used to forge/replay authentication.

**`folders`** *(Phase 2)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, `CASCADE`) | indexed |
| parent_folder_id | UUID (FK → folders.id, `CASCADE`), nullable | self-referential; null = top-level |
| name | VARCHAR(255) | |
| path | VARCHAR(4096) | materialized path, e.g. `/Documents/Projects`; indexed |
| level | INTEGER | depth, 0 = top-level; denormalized for cheap reads |
| is_root | BOOLEAN | true iff `parent_folder_id IS NULL` |
| is_deleted / deleted_at / deleted_by | soft-delete trio | trash state (see `SoftDeleteMixin`) |
| created_by / updated_by | UUID (FK → users.id, `SET NULL`), nullable | audit trail (see `AuditMixin`) |
| created_at / updated_at | TIMESTAMPTZ | |

Unique constraint: `(owner_id, parent_folder_id, name)` **partial index** — only enforced
`WHERE is_deleted = false`, so a trashed "Reports" doesn't block creating a new one
in the same location.

**`file_metadata`** *(Phase 2 core + Phase 3 storage columns)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, `CASCADE`) | indexed |
| folder_id | UUID (FK → folders.id, `CASCADE`), nullable | null = top-level |
| original_filename | VARCHAR(255) | user-facing name |
| stored_filename | VARCHAR(512), unique | per-row unique key reservation (legacy Phase 2 field; distinct from `object_name` — see below) |
| extension | VARCHAR(32), nullable | derived from filename |
| mime_type | VARCHAR(255), nullable | server-detected from content, not trusted from the client |
| size | BIGINT | current size in bytes |
| checksum | VARCHAR(128), nullable, indexed | SHA-256 of the file's content; drives duplicate detection |
| version | INTEGER | current version pointer (full history in `file_versions`) |
| status | ENUM(`reserved`,`active`,`archived`) | `reserved` = metadata-only (Phase 2); `active` = bytes uploaded (Phase 3) |
| **storage_provider** | VARCHAR(32), nullable | `"gcs"` today; abstraction point for a future backend |
| **bucket_name** | VARCHAR(255), nullable | which GCS bucket the object lives in |
| **object_name** | VARCHAR(1024), nullable, indexed (NOT unique) | the actual GCS object key — see "Object Naming" below |
| **public_url** | VARCHAR(2048), nullable | reserved for future use; always `NULL` today (buckets are private, see Security) |
| **storage_class** | VARCHAR(32), nullable | GCS storage class reported after upload (e.g. `STANDARD`) |
| **etag** | VARCHAR(255), nullable | GCS object etag, for optimistic integrity checks |
| **upload_status** | ENUM(`pending`,`completed`,`failed`), indexed | lifecycle of the *bytes*, independent of `status` (the *row*) |
| **uploaded_at** | TIMESTAMPTZ, nullable | when the bytes were confirmed persisted |
| is_deleted / deleted_at / deleted_by | soft-delete trio | |
| created_by / updated_by | UUID, nullable | audit trail |
| created_at / updated_at | TIMESTAMPTZ | |

Unique constraint: `(owner_id, folder_id, original_filename)`, partial on `is_deleted = false`
— same pattern as folders.

**Why `object_name` is NOT unique**: content-based duplicate detection (see
"Duplicate Detection" below) intentionally lets two different `file_metadata`
rows point at the *same* GCS object when their content is byte-identical.
`stored_filename` stays unique per-row (it's a Phase 2 legacy reservation),
but the physical `object_name` it may share is a many-to-one relationship
by design.

**`file_versions`** *(Phase 2)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| file_id | UUID (FK → file_metadata.id, `CASCADE`) | indexed |
| version | INTEGER | unique per `file_id` |
| checksum / size | snapshot at that version | |
| created_at | TIMESTAMPTZ | |

Append-only. `file_metadata.version/checksum/size` always mirrors the latest row
here; this table exists purely to power `GET /metadata/{id}/versions`.

## 5. API Design

All endpoints are namespaced under `/api/v1`.

**Auth & Users** *(Phase 1)*

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | App/DB/Redis health status |
| POST | `/auth/register` | none | Create a new user |
| POST | `/auth/login` | none | OAuth2 password flow → access + refresh token |
| POST | `/auth/refresh` | none (valid refresh token) | Rotate refresh token, issue new pair |
| POST | `/auth/logout` | none (valid refresh token) | Revoke a refresh token |
| GET | `/users/me` | Bearer access token | Current user's profile |
| GET | `/users/{id}` | Bearer access token, **admin role** | Any user's profile |

**Folders** *(Phase 2, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/folders` | Create a folder (top-level if `parent_folder_id` omitted) |
| GET | `/folders` | List child folders of a parent (or top-level if omitted) |
| GET | `/folders/tree` | Full nested folder tree (forest, or rooted at one folder) |
| GET | `/folders/breadcrumb?folder_id=` | Ancestor chain from root to the given folder |
| GET | `/folders/trash` | List trashed folders |
| GET | `/folders/{id}` | Get a folder by ID |
| PUT | `/folders/{id}` | Rename |
| POST | `/folders/{id}/move` | Move to a new parent |
| DELETE | `/folders/{id}` | Soft delete (moves to trash, cascades to descendants) |
| POST | `/folders/{id}/restore` | Restore from trash |
| DELETE | `/folders/{id}/permanent` | Permanently delete (must be trashed first; irreversible) |

**File Metadata** *(Phase 2, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/metadata` | Create a metadata placeholder (reserves storage slot; no upload) |
| GET | `/metadata/search` | Search/filter/sort/paginate files |
| GET | `/metadata/trash` | List trashed files |
| GET | `/metadata/{id}` | Get file metadata by ID |
| PUT | `/metadata/{id}` | Update metadata (size/checksum change bumps version) |
| POST | `/metadata/{id}/rename` | Rename |
| POST | `/metadata/{id}/move` | Move to a new folder |
| DELETE | `/metadata/{id}` | Soft delete (moves to trash) |
| POST | `/metadata/{id}/restore` | Restore from trash |
| DELETE | `/metadata/{id}/permanent` | Permanently delete (must be trashed first; irreversible) |
| GET | `/metadata/{id}/versions` | Full version history |

**Files — Cloud Storage** *(Phase 3, all require Bearer auth)*

| Method | Path | Description |
|---|---|---|
| POST | `/files/upload` | Multipart upload: bytes → GCS, metadata → Postgres, in one call |
| GET | `/files/{id}/download` | Stream a file's bytes; supports `Range` requests |
| GET | `/files/{id}/signed-url` | Time-boxed V4 signed URL for direct, temporary access |
| PUT | `/files/{id}/replace` | Upload new bytes as the next version |
| DELETE | `/files/{id}/permanent` | Delete bytes from GCS **and** the metadata row (must be trashed first) |

Note the split with `/metadata`: `DELETE /metadata/{id}` (Phase 2) soft-deletes
a row without touching storage — a trashed file's bytes stay recoverable via
`POST /metadata/{id}/restore`. `DELETE /files/{id}/permanent` (Phase 3) is what
actually reclaims storage, and only after the row is already trashed.

<details>
<summary>Files API examples</summary>

```bash
# Upload
curl -X POST http://localhost:8000/api/v1/files/upload \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "file=@report.pdf" \
  -F "folder_id=<optional-folder-uuid>"

# Download (streaming, supports Range)
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  http://localhost:8000/api/v1/files/<file_id>/download -o report.pdf

# Resume/range download
curl -H "Authorization: Bearer $ACCESS_TOKEN" -H "Range: bytes=0-1023" \
  http://localhost:8000/api/v1/files/<file_id>/download

# Signed URL (defaults to SIGNED_URL_EXPIRATION_MINUTES)
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  "http://localhost:8000/api/v1/files/<file_id>/signed-url?expires_in_minutes=60"

# Replace (new version)
curl -X PUT http://localhost:8000/api/v1/files/<file_id>/replace \
  -H "Authorization: Bearer $ACCESS_TOKEN" -F "file=@report-v2.pdf"

# Permanently delete (must already be trashed via DELETE /metadata/{id})
curl -X DELETE http://localhost:8000/api/v1/files/<file_id>/permanent \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

</details>

**Trash** *(Phase 2)*

| Method | Path | Description |
|---|---|---|
| GET | `/trash` | Combined view: everything currently trashed (folders + files) |

Every response follows the standard envelope:

```json
{
  "success": true,
  "message": "Request completed successfully",
  "data": {},
  "errors": null,
  "timestamp": "2026-07-29T10:00:00Z",
  "request_id": "b3b3b3b3-1234-4567-8901-abcdefabcdef"
}
```

## 6. Authentication Flow

1. **Register** → `POST /auth/register` creates a user with a bcrypt-hashed password.
2. **Login** → `POST /auth/login` (OAuth2 password form: `username`=email, `password`)
   returns a short-lived **access token** (15 min default) and a longer-lived
   **refresh token** (7 days default). The refresh token's `jti` is persisted.
3. **Access protected routes** → send `Authorization: Bearer <access_token>`.
   `get_current_user` decodes the JWT and re-checks `is_active` against the DB
   on every request, so deactivation takes effect immediately.
4. **Refresh** → `POST /auth/refresh` with the refresh token. The server verifies
   the token's `jti` hasn't been revoked, **revokes it**, and issues a brand-new
   access+refresh pair (**rotation**). Replaying an already-used refresh token
   is rejected — this bounds the damage from a leaked refresh token to one use.
5. **Logout** → `POST /auth/logout` revokes the given refresh token's `jti`.
   Access tokens are stateless and simply expire naturally; there is no
   server-side access-token revocation list in Phase 1 by design.
6. **Role-based authorization** → `require_role(UserRole.ADMIN)` is a dependency
   factory used on routes that need it (e.g. `GET /users/{id}`), so authorization
   requirements are visible in the route signature.

## 7. Folder Hierarchy & Materialized Paths *(Phase 2)*

Folders form a self-referential tree via `parent_folder_id`, with no fixed
depth limit. Each folder also stores a **materialized path**
(e.g. `/Documents/Projects/AI`) and a **level** (depth, 0 = top-level),
kept in sync automatically:

- **Create**: `path = parent.path + '/' + name`, `level = parent.level + 1`.
- **Rename**: the folder's own path is rewritten, and every descendant's
  path is rewritten too (prefix replacement), in one repository call.
- **Move**: the folder's path/level are recomputed against the new parent,
  descendants get both a path-prefix rewrite and a level shift.

This trades a bit of extra work on rename/move for very cheap reads —
breadcrumbs, subtree queries, and "is X inside Y" checks are simple string
operations instead of recursive queries.

**Validation enforced before any move/rename lands:**
- No duplicate folder name within the same parent (soft-deleted siblings excluded)
- No moving a folder into itself
- No moving a folder into one of its own descendants (circular reference)
- Folder names reject empty strings, `.`/`..`, and path-separator characters

## 8. Trash & Soft Delete *(Phase 2)*

There is **no separate trash table**. Both `folders` and `file_metadata`
carry `is_deleted` / `deleted_at` / `deleted_by` directly (via
`SoftDeleteMixin`). This means:

- **Delete** = flip `is_deleted = true`. Deleting a folder cascades the
  same flag to every descendant folder (and, once created inside it,
  every descendant file) so a trashed folder's contents move with it.
- **Restore** = flip it back. Restoring a folder restores the descendants
  that were trashed alongside it, without resurrecting anything that was
  independently trashed earlier.
- **Permanent delete** = the *only* operation that issues a real SQL
  `DELETE`, and it requires the item to already be in the trash — you
  cannot skip straight from active to permanently gone.

## 9. Search, Pagination, Sorting, Filtering *(Phase 2)*

`GET /metadata/search` supports all of the following simultaneously:

- **Free-text search (`q`)**: matches the file's own name OR the name of
  the folder it lives in.
- **Filters**: `folder_id`, `extension`, `mime_type`, `version`,
  `is_deleted`, `created_after`/`created_before`, `updated_after`/`updated_before`.
- **Sorting**: `sort_by` (name, created_at, updated_at, size, type) ×
  `sort_order` (asc/desc).
- **Pagination**: `page` / `page_size` (max 100), returning `total`,
  `total_pages`, `has_next`, `has_previous` alongside `items`.

The same `Page[T]` / `PaginationParams` pair is reused by every list-style
endpoint, so pagination behaves identically across the whole API.

## 10. Cloud Storage Architecture *(Phase 3)*

Bytes and metadata are deliberately split across two systems:

```
Client --multipart--> FastAPI --metadata--> PostgreSQL (file_metadata, file_versions)
                          |
                          +--bytes--> Google Cloud Storage (private bucket)
```

`app/services/storage_service.py::StorageService` is the **only** module that
imports `google.cloud.storage` — nothing else touches a `Blob`/`Bucket`
directly. `app/services/file_upload_service.py::FileUploadService` is the
only orchestrator that talks to both `StorageService` and
`FileMetadataRepository` in the same operation, which is where the
two-systems consistency problem is actually solved.

### Object Naming

Uploaded bytes are **never** stored under the user's original filename.
Instead, `StorageService.generate_object_name()` builds:

```
{tenant}/{owner_id}/{year}/{month}/{uuid4}.{extension}
```

Why:
1. **Path/collision safety** — user-controlled filenames can contain path
   traversal segments, unicode tricks, or simply collide with another file
   (two users can both have `"invoice.pdf"`); a UUID guarantees a globally
   unique, injection-safe key regardless of what anyone names their files.
2. **Sharding** — the `{owner}/{year}/{month}/…` prefix gives GCS's internal
   load balancing a natural partition key, and makes "list this user's
   objects from a given month" cheap for future lifecycle/archival tooling.
3. **Stability** — renaming a file (`original_filename`) never requires
   renaming the underlying object; the object name is permanent for the
   object's lifetime, exactly mirroring the `stored_filename` reservation
   Phase 2 already established.

### Upload Flow & Consistency

```
validate folder exists
  -> validate + hash the upload (filename, size, sniffed MIME, extension)
  -> reject duplicate filename-in-folder (before spending a network upload)
  -> check for identical content already uploaded by this owner (dedup)
  -> upload bytes to GCS (skipped if deduped)
  -> persist FileMetadata (status=active) + FileVersion(v1)
```

**Rollback strategy**: if metadata persistence fails *after* a real
(non-deduped) upload already succeeded, the just-uploaded object is deleted
so no orphaned, unreferenced object is left in the bucket. If that rollback
delete itself fails, `RollbackFailedException` is raised (rather than
silently swallowing the error) — an operator being paged on it is better
than silent drift between Postgres and GCS.

### Duplicate Detection

SHA-256 is computed by streaming the upload once (same pass used to sniff
its MIME type). If a non-deleted `FileMetadata` row for the same owner
already has that checksum, the new row is created pointing at the **same**
`object_name` — no bytes are re-uploaded. This is deliberate
content-addressable-storage behavior: it saves storage cost and upload
bandwidth for the common case of the same file landing in two folders, or a
client-side retry. The trade-off — one object can now be referenced by more
than one row — is exactly why permanent delete (below) checks for other
referencing rows before deleting the object itself.

### Download Flow & Signed URLs

`GET /files/{id}/download` streams bytes back to the client via
`StreamingResponse`, chunk by chunk, without buffering the whole file in
memory — real streaming, not `download_as_bytes()` into a response body.
An HTTP `Range` header is honored (returns `206 Partial Content` with
`Content-Range`), so range-friendly clients (video/audio scrubbing, download
managers, resumable downloads) work correctly.

`GET /files/{id}/signed-url` issues a V4 signed URL — a time-boxed
(`SIGNED_URL_EXPIRATION_MINUTES`, overridable per-request), pre-authenticated
link straight to the object in GCS. This is the **only** sanctioned way a
client gets direct object access; the bucket itself is never public (see
Security, below).

### Replace (New Version)

Replacing a file's content uploads to a **brand-new** object name, swaps
`FileMetadata` to point at it, bumps `version`, and only *then* deletes the
old object (skipped if another row still references it via dedup). Doing it
in this order means a crash at any point still leaves either the old
object+row (untouched) or the new object+row (fully switched over)
consistent — never a state where metadata points at bytes that don't exist.

### Deletion Strategy

- **Soft delete** (`DELETE /metadata/{id}`, unchanged from Phase 2) — flips
  `is_deleted`. The GCS object is left alone, so `POST /metadata/{id}/restore`
  can bring the file back with its bytes intact.
- **Permanent delete** (`DELETE /files/{id}/permanent`, Phase 3) — requires
  the row to already be trashed, then deletes the database row and, only if
  no other row still references the same object (dedup safety check),
  deletes the GCS object too.

### Security

- **Private buckets, always.** Uniform bucket-level access, no `allUsers`/
  `allAuthenticatedUsers` IAM bindings, ever. `public_url` exists as a schema
  column for a possible future CDN-fronted use case but is never populated
  today — the only sanctioned access path is a signed URL or an
  authenticated `/files/{id}/download` request.
- **IAM, least privilege.** The app's service account gets
  `roles/storage.objectAdmin` scoped to its one bucket (see GCS Setup
  above), never project-wide `roles/storage.admin`.
- **Signed URLs are time-boxed**, default 15 minutes, capped at 7 days
  (`expires_in_minutes` query param, `le=10080`) — long enough to be useful,
  short enough that a leaked URL (logged, forwarded, cached by a proxy)
  has a bounded blast radius.
- **Input validation happens before any bytes reach GCS**: filename
  (length/characters), size (`MAX_UPLOAD_SIZE_MB`), extension
  (`BLOCKED_EXTENSIONS`), and MIME type are all checked pre-upload — see
  Upload Flow above.
- **MIME type is sniffed from content**, not trusted from the client's
  `Content-Type` header or the filename extension — both are trivially
  spoofable (e.g. renaming `payload.exe` to `photo.jpg`).
- **Virus scanning is a placeholder for a future phase.** No AV engine is
  wired in yet; `FileValidationService` is the intended integration point
  — a scan step would slot in after content-sniffing and before the GCS
  upload call, with the same "reject before any bytes are written" ordering
  everything else here follows.

## 11. Distributed Backend Architecture *(Phase 4)*

Phase 4 turns NimbusFS from "one FastAPI process" into "N interchangeable
FastAPI replicas behind a load balancer," in preparation for a Kubernetes/
Cloud Run deployment in Phase 5. No Kubernetes, autoscaling, or deployment
manifests exist yet — this phase is purely about making the *application
itself* safe to run that way.

```
                     Clients
                         │
                         ▼
              Google Cloud Load Balancer
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
   FastAPI #1      FastAPI #2      FastAPI #3
        │                │                │
        └────────────────┼────────────────┘
                         │
                  Shared Redis
                         │
                 Shared PostgreSQL
                         │
              Google Cloud Storage
```

### Stateless design

No server may hold state a client's *next* request depends on. Concretely:

| Never stored on a server | Where it actually lives instead |
|---|---|
| Sessions | Nowhere — `access`/`refresh` JWTs (Phase 1) carry all session state |
| Uploaded file bytes | Google Cloud Storage (Phase 3) |
| Temporary upload buffers | Streamed straight through to GCS, never written to local disk |
| Application cache | Redis (shared, Phase 4 plumbing only — no metadata caching yet) |
| In-flight request coordination | Redis (distributed locks, idempotency-key records) |

This is what makes horizontal scaling trivial: since no replica has any
memory a client's next request depends on, a load balancer can route each
request to *any* replica, replicas can be added/removed at will, and a
crashed replica loses nothing but its own in-flight requests (which the
client's own retry — see Idempotency below — makes safe to redo against a
different replica).

### Request flow

```
Client
  -> Load Balancer            (routes to any healthy, ready replica)
  -> FastAPI instance         (any one — fully interchangeable)
  -> TrustedProxyMiddleware   (resolves real client IP/scheme from X-Forwarded-*)
  -> RequestContextMiddleware (request/correlation/trace IDs, structured logging)
  -> Authentication           (JWT verified locally — no session store lookup)
  -> Business logic           (services/repositories — no server-local state)
  -> PostgreSQL                (single shared source of truth)
  -> Google Cloud Storage      (single shared bytes store)
  -> Response                 (X-Request-ID/X-Correlation-ID/X-Server-ID headers)
```

Every hop after the load balancer could have landed on a different replica
without changing the outcome — that's the whole point.

### Server identity

Every replica exposes its own identity (`app/core/server_info.py`) in
every log line and every health-check response:

| Field | Purpose |
|---|---|
| `instance_id` | Random UUID, generated fresh per process start |
| `hostname` | OS hostname — the pod/container name on GKE/Cloud Run |
| `process_id` | OS PID, for correlating with process-level metrics |
| `app_version` / `build_version` / `git_commit` | Exactly which build is running |
| `environment` | development/testing/staging/production |

### Distributed logging & correlation IDs

`RequestContextMiddleware` (`app/middleware/request_context.py`) generates
three distinct IDs per request and binds them into `structlog`'s
contextvars, so every log line anywhere in the call stack — routes,
services, repositories, the GCS storage layer — carries them automatically:

- **`request_id`** — unique per hop, always server-generated, never trusted
  from a client header.
- **`correlation_id`** — unique per end-to-end client operation, honored
  from an inbound `X-Correlation-ID` header if present (so a retried
  operation across multiple HTTP calls can be traced as one story),
  otherwise generated fresh.
- **`trace_id`** — reserved for OpenTelemetry/Cloud Trace integration in a
  future phase; honored from `X-Trace-ID` if supplied, defaults to the
  correlation ID otherwise so the field is always populated pre-OTel.

All three, plus `server_id`, are echoed back as response headers
(`X-Request-ID`, `X-Correlation-ID`, `X-Trace-ID`, `X-Server-ID`,
`X-Response-Time-Ms`) and emitted as JSON via `structlog`
(`app/logging/logger.py`, unchanged from Phase 1) — ready to ship straight
into Google Cloud Logging or any other JSON-log aggregator without a
parsing step; grep any of these IDs across every replica's logs to
reconstruct one request's full journey through a distributed fleet.

### Health, readiness & liveness

Three endpoints with three distinct probe semantics (`app/api/v1/health/routes.py`):

| Endpoint | Checks | Used for |
|---|---|---|
| `GET /health` | DB + Redis + Storage (deep) | Dashboards, humans, monitoring |
| `GET /ready` | DB + Redis + Storage (deep), `503` if any unhealthy | Load balancer / readiness probe — "route traffic here?" |
| `GET /live` | Nothing external — answers instantly | Liveness probe — "is this process alive?" |

Splitting `/ready` from `/live` matters: a slow database should pull a
replica out of the load balancer's rotation (`/ready` -> `503`), but must
never cause an otherwise-healthy process to be killed and restarted (which
is what a failing liveness probe triggers) — killing the process doesn't
fix a slow database, it just adds a restart storm on top of it.

### Startup & shutdown lifecycle

`app/main.py`'s `lifespan()`:

- **Startup**: connect DB -> connect Redis -> verify the GCS bucket exists
  -> log `application_ready`. Each check runs through the shared retry
  policy (`app/core/retry.py`, exponential backoff + full jitter) to
  absorb one transient blip (e.g. Postgres still finishing crash recovery
  during a coordinated restart). If a dependency is still unreachable
  after retrying, startup raises and the process exits non-zero —
  `Settings.FAIL_FAST_ON_STARTUP` (default `true`). A replica that can
  never reach its database should never accept traffic; a crash-looping
  pod is a far clearer operational signal than one silently serving 500s
  forever.
- **Shutdown**: close the Postgres connection pool, close the Redis
  connection pool, log `application_shutdown_complete`. Distributed locks
  are deliberately never explicitly released here — every lock has a
  bounded TTL (`app/core/distributed_lock.py`), so a replica that dies
  uncleanly (`kill -9`, OOM) self-heals within that TTL regardless of
  whether any shutdown hook got to run. In production, uvicorn's own
  `--timeout-graceful-shutdown` (set to >= `SHUTDOWN_GRACE_PERIOD_SECONDS`)
  is what actually stops routing new connections and drains in-flight ones
  before this hook runs — this hook is defense-in-depth, not the primary
  mechanism.

### Redis infrastructure

`app/database/redis.py` provides the shared connection pool every replica
draws from; three Phase 4 features build on it directly:

- **`app/core/distributed_lock.py`** — a `SET NX PX`-based lock with a
  Lua-scripted, token-checked release (so replica A can never release a
  lock replica B has since re-acquired after A's lock expired). TTL-based,
  not renewal-based — a deliberate simplicity trade-off documented in the
  module itself.
- **`app/services/idempotency_service.py`** — see Idempotency below.
- **`check_redis_connection()`** — retry-wrapped health check used by
  `/health`, `/ready`, and startup.

Metadata *caching* is explicitly **not** implemented yet — this phase
prepares the plumbing (pool, DI, health check, retry) only.

### Database improvements

- **Connection pooling**: `pool_size`/`max_overflow`/`pool_pre_ping`/
  `pool_recycle`/`pool_timeout` all explicitly tuned (`app/database/session.py`).
  Scaling ceiling documented inline: total Postgres connections used is
  `replica_count * (pool_size + max_overflow)`, which must stay under
  Postgres's own `max_connections` — a future-phase concern (PgBouncer, or
  raising `max_connections`) once replica count grows past what today's
  defaults allow.
- **Retry strategy**: `check_database_connection()` retries transient
  failures with the shared backoff policy.
- **Deadlock handling**: `run_with_deadlock_retry()` retries a unit of
  work specifically on Postgres's `40P01` deadlock SQLSTATE, and only
  that — any other `OperationalError` fails immediately, unretried.
- **Read/write separation & optimistic locking**: both are *designed*,
  documented in detail in `app/database/session.py`'s module docstring,
  and deliberately **not** wired up yet (no read-replica engine exists;
  no `row_version` column has been added to any model) — doing either
  half-heartedly here would either add operational complexity with no
  current benefit (a replica engine nothing routes to) or conflate two
  unrelated meanings of "version" on `FileMetadata` (see that docstring
  for why `FileMetadata.version`, the file's *content* version, can't
  double as a row-level optimistic-lock counter).

### Idempotent APIs

`POST /files/upload` accepts an optional `Idempotency-Key` header
(`app/services/idempotency_service.py`):

1. Client generates one UUID per *logical* upload attempt and sends it on
   every retry of that same attempt.
2. First request with a given key atomically claims it in Redis (`SET NX`)
   and proceeds; concurrent duplicates (a genuine race between two retries
   in flight) get `409 Conflict` — "already being processed" — rather than
   both executing the upload.
3. A request that completes has its response cached under that key
   (`IDEMPOTENCY_KEY_TTL_SECONDS`, default 24h); a later retry with the
   *same* key replays the exact original response instead of re-uploading.
4. A key reused with a different logical request (different filename/
   folder — see `compute_fingerprint`'s docstring for exactly what's
   fingerprinted and why) is rejected with `422` as a client bug, not
   silently replayed.
5. A request that genuinely fails releases its claim immediately, so the
   same key can be retried right away rather than waiting out the full TTL.

This composes with — but is distinct from — Phase 3's SHA-256 content-based
deduplication: idempotency prevents *duplicate uploads from network
retries*; content dedup prevents *duplicate storage bytes for identical
content*, however it got uploaded.

### Trusted proxies & forwarded headers

Behind Google Cloud Load Balancer, the ASGI server sees the LB as the TCP
peer, not the real client. `TrustedProxyMiddleware`
(`app/middleware/proxy_headers.py`) resolves the real client IP/scheme from
`X-Forwarded-For`/`X-Forwarded-Proto`, but only when the peer is in
`Settings.TRUSTED_PROXIES` (`*` for this phase's scope — pin to real proxy
CIDRs once the network topology is fixed) — never trusted unconditionally,
since that would let any direct client spoof its own IP in logs/rate-limit
keys.

### Retry, circuit breaker & rate-limit seams

- **`app/core/retry.py`** — generic async retry with exponential backoff +
  full jitter (jitter specifically to avoid a thundering herd when many
  replicas reconnect to a recovering dependency simultaneously). Used for
  DB/Redis/Storage health checks and startup verification.
- **`app/core/circuit_breaker.py`** — a minimal per-dependency, in-process
  (deliberately not Redis-shared — see module docstring) three-state
  breaker primitive. Provided as infrastructure this phase; broader
  application beyond the health-check paths is left to whichever future
  phase needs it.
- **`app/middleware/rate_limit.py`** — an explicit no-op placeholder that
  documents the seam a real Redis-backed limiter will occupy (`429` +
  `Retry-After`, keyed by client IP/user ID) — deferred past this phase,
  wired in now so the response contract (`X-RateLimit-*` headers) doesn't
  change later.

### Error handling

Every Phase 4 failure mode gets its own domain exception + handler
(`app/exceptions/`), mapped consistently onto the standard `APIResponse`
envelope: `LockAcquisitionException` (409), `CircuitBreakerOpenException`
(503), `ServiceUnavailableException` (503),
`IdempotencyKeyInProgressException` (409),
`IdempotencyKeyReplayedException` (422) — no endpoint ever returns a raw
stack trace or an inconsistent error shape for these.

## 12. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 13. Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development` / `testing` / `staging` / `production` |
| `JWT_SECRET_KEY` | HMAC signing secret — **must** be overridden in real deployments |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Token lifetimes |
| `POSTGRES_*` | Database connection |
| `REDIS_*` | Redis connection |
| `CORS_ALLOWED_ORIGINS` | Comma-separated list of allowed origins |
| `LOG_LEVEL` / `LOG_JSON` | Logging verbosity/format |
| `GCS_PROJECT_ID` / `GCS_BUCKET_NAME` | Google Cloud project + bucket for file storage |
| `GCS_CREDENTIALS_PATH` | Service-account key file path; **leave unset** in staging/production (uses Application Default Credentials / Workload Identity instead) |
| `SIGNED_URL_EXPIRATION_MINUTES` | Default signed-URL lifetime; overridable per-request |
| `MAX_UPLOAD_SIZE_MB` | Hard cap on upload size (rejected with `413`) |
| `ALLOWED_MIME_TYPES` | Comma-separated allowlist; empty = allow all except `BLOCKED_EXTENSIONS` |
| `BLOCKED_EXTENSIONS` | Comma-separated extension blocklist (executables/scripts by default) |
| `BUILD_VERSION` / `GIT_COMMIT` | Baked in at container build time; surfaced in logs + health endpoints (Phase 4) |
| `TRUSTED_PROXIES` | Comma-separated proxy IPs whose `X-Forwarded-*` headers are honored; `*` trusts any (Phase 4) |
| `IDEMPOTENCY_KEY_TTL_SECONDS` | How long a completed `Idempotency-Key` response is replayable (Phase 4) |
| `LOCK_DEFAULT_TTL_SECONDS` | Default TTL for Redis-backed distributed locks (Phase 4) |
| `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY_SECONDS` / `RETRY_MAX_DELAY_SECONDS` | Backoff policy for transient DB/Redis/Storage failures (Phase 4) |
| `FAIL_FAST_ON_STARTUP` | Refuse to finish startup if a critical dependency is unreachable after retrying (Phase 4) |
| `SHUTDOWN_GRACE_PERIOD_SECONDS` | Ceiling on graceful shutdown; match/exceed with uvicorn's `--timeout-graceful-shutdown` (Phase 4) |

### Google Cloud Storage Setup

```bash
# 1. Create the bucket (uniform bucket-level access, never public)
gsutil mb -p <PROJECT_ID> -l <REGION> -b on gs://nimbusfs-files-dev

# 2. Create a service account for local development
gcloud iam service-accounts create nimbusfs-storage \
  --display-name "NimbusFS Storage (dev)"

# 3. Grant it object-level access ONLY to this bucket (not project-wide Storage Admin)
gsutil iam ch \
  serviceAccount:nimbusfs-storage@<PROJECT_ID>.iam.gserviceaccount.com:roles/storage.objectAdmin \
  gs://nimbusfs-files-dev

# 4. For local dev only: export a key and point GCS_CREDENTIALS_PATH at it.
#    Never do this in staging/production — see below.
gcloud iam service-accounts keys create ./gcs-dev-key.json \
  --iam-account nimbusfs-storage@<PROJECT_ID>.iam.gserviceaccount.com
```

**IAM permissions**: grant `roles/storage.objectAdmin` scoped to the single
bucket (via `gsutil iam ch` above), not `roles/storage.admin` on the project
— the app only ever needs to create/read/delete objects inside its own
bucket, never manage buckets or IAM policy itself.

**Credentials in staging/production**: don't ship a key file in the
container image or mount one as a Kubernetes secret. Instead, bind the
GKE service account to the IAM service account via Workload Identity, leave
`GCS_CREDENTIALS_PATH` unset, and `storage.Client()` picks up Application
Default Credentials automatically — this is what makes `app/database/gcs.py`
work unchanged across environments.

## 15. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 16. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 17. Database Migrations (Alembic)

```bash
# Generate a new migration from model changes
alembic revision --autogenerate -m "describe the change"

# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1
```

Migrations so far:
- `0001_initial` — `users`, `refresh_tokens`
- `0002_metadata` — `folders`, `file_metadata`, `file_versions`
- `0003_storage` — adds `storage_provider`, `bucket_name`, `object_name`,
  `public_url`, `storage_class`, `etag`, `upload_status`, `uploaded_at` to
  `file_metadata`

## 18. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 19. Testing

Tests run against an isolated in-memory SQLite database — no external
services required.

```bash
pytest -v
```

Coverage includes:
- **Phase 1**: registration, login, protected routes, role-based authorization,
  refresh-token rotation (including replay rejection), logout, health endpoint.
- **Phase 2**: folder CRUD, nested creation, duplicate-name rejection, rename
  with cascading path updates, move (including circular-reference and
  move-into-self rejection), folder tree, breadcrumb, folder/file trash +
  restore + permanent delete, file metadata CRUD, rename, move, version
  bumping on content change, search (filename, folder-name, extension,
  MIME type, deleted status), sorting, pagination, and cross-owner data
  isolation.
- **Phase 3** (`tests/test_file_storage.py`, against an in-memory
  `FakeGCSClient` — see `tests/fakes/fake_gcs.py`, never real GCS): upload
  success, upload into a nonexistent folder (404), duplicate filename in
  the same folder (409), empty file rejection (400), blocked extension
  rejection (415), content-based duplicate detection/deduplication,
  streaming download round-trip, HTTP Range requests (206), downloading a
  metadata-only (never-uploaded) file (404), signed URL generation (default
  and custom expiration), replace (version bump + object swap + old object
  cleanup), permanent delete (object + row removed), permanent-delete
  safety when an object is still shared via dedup, rollback of an orphaned
  object when metadata persistence fails after a successful upload, a
  simulated storage-backend outage (502), and cross-owner access isolation.
- **Phase 4** (`tests/test_distributed.py` + additions to
  `tests/test_health.py`, against an in-memory `FakeRedisClient` — see
  `tests/fakes/fake_redis.py`, never real Redis): idempotency-key replay
  on retry, idempotency-key reused with a different payload (422),
  concurrent requests sharing one idempotency key (never two uploads),
  the `IdempotencyService` unit contract (proceed/replay/fail), distributed
  lock acquire/release/conflict and token-safety (a released lock never
  clobbers a different holder's re-acquired one), retry-with-backoff
  (transient success, exhaustion, non-retryable exceptions skip retry),
  circuit breaker state transitions (closed -> open -> half-open),
  `/health`/`/ready`/`/live` response shape, correlation/trace/server-ID
  headers and their propagation from an inbound `X-Correlation-ID`,
  graceful degradation (a dependency check that itself raises still
  returns a structured `200`/`503`, never a `500`), and many-concurrent-
  requests-get-unique-IDs as a statelessness sanity check. Note:
  `/health`/`/ready` intentionally check *real* DB/Redis connectivity
  (not the SQLite/fake overrides) — see `tests/conftest.py` for how the
  suite keeps that fast and deterministic without requiring real infra.

## 20. Future Roadmap (Phases 5–15, not yet built)

Kubernetes/GKE deployment, autoscaling (HPA), chunked/resumable uploads,
sharing & permissions between users, virus scanning integration, thumbnail
generation, full-text content search, Pub/Sub-driven background workers,
real rate limiting, Redis metadata caching, CI/CD via GitHub Actions,
Terraform IaC, Cloud Armor, Cloud CDN, observability (Cloud Monitoring/
Logging dashboards, OpenTelemetry tracing).

## 21. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.