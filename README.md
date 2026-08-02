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

**`file_metadata`** *(Phase 2)*

| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| owner_id | UUID (FK → users.id, `CASCADE`) | indexed |
| folder_id | UUID (FK → folders.id, `CASCADE`), nullable | null = top-level |
| original_filename | VARCHAR(255) | user-facing name |
| stored_filename | VARCHAR(512), unique | **reserved** future storage object key — no bytes exist yet |
| extension | VARCHAR(32), nullable | derived from filename |
| mime_type | VARCHAR(255), nullable | |
| size | BIGINT | declared/current size in bytes |
| checksum | VARCHAR(128), nullable | current version's checksum |
| version | INTEGER | current version pointer (full history in `file_versions`) |
| status | ENUM(`reserved`,`active`,`archived`) | `reserved` until a future upload phase confirms bytes landed |
| is_deleted / deleted_at / deleted_by | soft-delete trio | |
| created_by / updated_by | UUID, nullable | audit trail |
| created_at / updated_at | TIMESTAMPTZ | |

Unique constraint: `(owner_id, folder_id, original_filename)`, partial on `is_deleted = false`
— same pattern as folders.

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

## 10. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 11. Environment Variables

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

## 12. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 13. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 14. Database Migrations (Alembic)

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

## 15. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 16. Testing

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

## 17. Future Roadmap (Phases 3–15, not yet built)

Actual file upload/download, chunked/multipart uploads, Google Cloud Storage
integration (wiring `stored_filename` to real objects), sharing & permissions,
Pub/Sub-driven background workers, virus scanning, thumbnails, full-text
content search, rate limiting, Redis caching, CI/CD via GitHub Actions,
Terraform IaC, GKE deployment, Cloud Armor, Cloud CDN, observability
(Cloud Monitoring/Logging dashboards).

## 18. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.