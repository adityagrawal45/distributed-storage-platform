
# NimbusFS

**A Cloud-Native Distributed File Storage Platform built with Python, FastAPI and Google Cloud.**

> **Phase 1 of 15** — Backend Foundation: Clean Architecture, Auth, PostgreSQL, Redis, Docker, Testing.
> File upload/storage features are **not yet implemented** — they arrive in later phases.

---

## 1. Project Overview

NimbusFS is being built incrementally into a Google Drive / Dropbox-like distributed
file storage platform, deployed on GKE. Phase 1 establishes the production-grade
backend foundation everything else will be built on:

- Clean Architecture with strict layer separation
- JWT authentication with refresh-token rotation
- Role-based authorization
- PostgreSQL via SQLAlchemy 2.0 (async) + Alembic migrations
- Redis connection (plumbing only — no caching logic yet)
- Structured JSON logging, global exception handling, standardized API responses
- Dockerized local development environment
- Full pytest suite

## 2. Architecture

```
Client
  │
  ▼
FastAPI App (app/main.py)
  │  ── Middleware: CORS → TrustedHost → SecurityHeaders → RequestContext
  │  ── Exception Handlers: validation, auth, domain, DB, unhandled
  ▼
API Layer          app/api/v1/{auth,users,health}/routes.py
  │  (parses input, calls services, wraps output in APIResponse)
  ▼
Service Layer       app/services/*.py
  │  (business logic: registration, login, token rotation, RBAC checks)
  ▼
Repository Layer    app/repositories/*.py
  │  (all persistence access; no business logic)
  ▼
Database Layer       app/database/{session,redis}.py
  │
  ▼
PostgreSQL (SQLAlchemy 2.0 async + asyncpg)      Redis (connection only)
```

Cross-cutting concerns live in `app/core` (config, security), `app/schemas`
(Pydantic models), `app/exceptions`, `app/logging`, and `app/dependencies`
(the FastAPI dependency-injection wiring).

Business logic never lives in route handlers — routes are thin, services
own all rules, repositories own all queries. This is enforced structurally,
not just by convention.

## 3. Folder Structure

```
nimbusfs/
├── app/
│   ├── api/v1/
│   │   ├── auth/routes.py        # register, login, refresh, logout
│   │   ├── users/routes.py       # /me, /{id} (admin-only)
│   │   ├── health/routes.py      # GET /health
│   │   └── router.py             # aggregates all v1 routers
│   ├── core/
│   │   ├── config/settings.py    # Pydantic Settings (env-driven config)
│   │   └── security/             # password hashing, JWT creation/verification
│   ├── database/
│   │   ├── session.py            # async SQLAlchemy engine/session (Unit of Work)
│   │   └── redis.py              # Redis connection pool
│   ├── models/                   # SQLAlchemy 2.0 declarative models (User, RefreshToken)
│   ├── repositories/             # DB access only, no business logic
│   ├── services/                 # business logic (AuthService, UserService)
│   ├── schemas/                  # Pydantic request/response models
│   ├── dependencies/             # DI providers + auth/RBAC dependencies
│   ├── middleware/                # request-ID logging, security headers
│   ├── exceptions/                # domain exceptions + global handlers
│   ├── logging/                   # structlog JSON logging config
│   └── main.py                    # app composition (the only "wiring" file)
├── alembic/                       # migrations
├── docker/Dockerfile
├── docker-compose.yml
├── tests/                         # pytest suite
├── scripts/                       # run_dev.sh, migrate.sh
├── requirements.txt
├── .env.example
└── README.md
```

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

## 5. API Design

All endpoints are namespaced under `/api/v1`.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | App/DB/Redis health status |
| POST | `/auth/register` | none | Create a new user |
| POST | `/auth/login` | none | OAuth2 password flow → access + refresh token |
| POST | `/auth/refresh` | none (valid refresh token) | Rotate refresh token, issue new pair |
| POST | `/auth/logout` | none (valid refresh token) | Revoke a refresh token |
| GET | `/users/me` | Bearer access token | Current user's profile |
| GET | `/users/{id}` | Bearer access token, **admin role** | Any user's profile |

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
   server-side access-token revocation list in Phase 1 by design — see the
   "Logout Design" note in `app/services/auth_service.py`.
6. **Role-based authorization** → `require_role(UserRole.ADMIN)` is a dependency
   factory used on routes that need it (e.g. `GET /users/{id}`), so authorization
   requirements are visible in the route signature.

## 7. Installation

```bash
git clone <repo-url> nimbusfs && cd nimbusfs
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit secrets, especially JWT_SECRET_KEY
```

## 8. Environment Variables

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

## 9. Running Locally (without Docker)

Requires a local PostgreSQL and Redis instance matching your `.env`.

```bash
alembic upgrade head
./scripts/run_dev.sh
# or: uvicorn app.main:app --reload
```

API available at `http://localhost:8000`, docs at `http://localhost:8000/docs`.

## 10. Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts the API, PostgreSQL, and Redis with health checks and a shared
network. Apply migrations inside the running container:

```bash
docker compose exec api alembic upgrade head
```

## 11. Database Migrations (Alembic)

```bash
# Generate a new migration from model changes
alembic revision --autogenerate -m "describe the change"

# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1
```

The Phase 1 baseline migration (`0001_initial`) creates `users` and
`refresh_tokens`.

## 12. API Documentation

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Raw OpenAPI schema: `GET /openapi.json`

(Docs are automatically disabled when `ENVIRONMENT=production`.)

## 13. Testing

Tests run against an isolated in-memory SQLite database — no external
services required.

```bash
pytest -v
```

Coverage includes: registration (success/duplicate/validation), login
(success/wrong password/nonexistent user), protected routes (valid/missing/
invalid token), role-based authorization, refresh-token rotation (including
replay rejection), logout, and the health endpoint.

## 14. Future Roadmap (Phases 2–15, not yet built)

File upload/download, chunked/multipart uploads, Google Cloud Storage
integration, file metadata & versioning, sharing & permissions, Pub/Sub-driven
background workers, virus scanning, thumbnails, full-text search, rate
limiting, caching, CI/CD via GitHub Actions, Terraform IaC, GKE deployment,
Cloud Armor, Cloud CDN, observability (Cloud Monitoring/Logging dashboards).

## 15. Contribution Guide

1. Create a feature branch from `main`.
2. Keep business logic in `services/`, persistence in `repositories/` — never
   in route handlers.
3. Add/extend tests for any behavior change; run `pytest` before opening a PR.
4. Run `alembic revision --autogenerate` for any model change and commit the
   generated migration alongside the model change.
5. Follow existing typing/async/PEP8 conventions.
=======
Distributed File Storage System – Phase 1
A production-ready foundation for a distributed file storage system, built with FastAPI, PostgreSQL, Redis, and Docker, following Clean Architecture.

Overview
This project is a scalable backend service that supports user authentication (JWT with refresh tokens), role-based access, and a health-check endpoint. It is designed to evolve into a multi-pod Kubernetes deployment with Google Cloud services.

Architecture
The code follows Clean Architecture with clear separation:

API Layer: Routes and dependencies.

Domain: SQLAlchemy models and Pydantic schemas.

Services: Business logic.

Repositories: Data access abstraction.

Infrastructure: DB, Redis, logging, config.

All components are decoupled for testability and maintainability.

Folder Structure
text
app/...
tests/...
migrations/...
docker-compose.yml
Dockerfile
README.md
Setup Instructions
Prerequisites
Python 3.12+

Docker & Docker Compose

(Optional) PostgreSQL and Redis locally

Environment Variables
Copy .env.example to .env and adjust secrets (especially SECRET_KEY).

Running Locally (without Docker)
Create virtual environment:

bash
python -m venv venv
source venv/bin/activate
Install dependencies:

bash
pip install -r requirements.txt
Run PostgreSQL and Redis (or use Docker for them only).

Apply migrations:

'''bash
alembic upgrade head
Run the server:

bash
uvicorn app.main:app --reload
API docs at http://localhost:8000/docs.

Running with Docker Compose
Ensure .env is set.

Build and start:

bash
docker-compose up -d --build
Wait for services to be healthy.

Access API at http://localhost:8000.

Testing
Run tests with pytest:

bash
pytest -v
API Documentation
Swagger UI: http://localhost:8000/docs

ReDoc: http://localhost:8000/redoc

Authentication Flow
Register via /api/v1/auth/register with email, name, password.

Login via /api/v1/auth/login to obtain access & refresh tokens.

Use access token in Authorization: Bearer <token> for protected endpoints.

When access token expires, use /api/v1/auth/refresh with refresh token to get a new pair.

Logout: client discards tokens (future: blacklist).

Future Roadmap
File upload/download with chunking.

Google Cloud Storage integration.

Kubernetes deployment with horizontal scaling.

Background workers for async processing.

File sharing and versioning.

Enhanced monitoring and observability.

Design Decisions
Clean Architecture: ensures business logic is independent of frameworks.

Async SQLAlchemy: for non-blocking I/O.

JWT with refresh tokens: stateless authentication, scalable.

Repository pattern: simplifies swapping databases.

Structured logging: JSON logs for ingestion into ELK/Cloud Logging.

License
Proprietary.


