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

