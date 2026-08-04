# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------
# Stage 1: builder — installs dependencies into an isolated venv layer
# ---------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Build deps needed to compile psycopg2 / bcrypt wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------
# Stage 2: runtime — slim final image, no build toolchain
# ---------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Populated by CI/CD via `docker build --build-arg GIT_COMMIT_SHA=$(git rev-parse HEAD) ...`.
# Surfaced in /health, logs, and X-Server-ID-adjacent identity fields —
# see app/core/server_identity.py. Defaults keep local `docker build`
# (no build-args) working unchanged.
ARG BUILD_VERSION=dev
ARG GIT_COMMIT_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    BUILD_VERSION=${BUILD_VERSION} \
    GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

# Runtime-only shared lib needed by asyncpg/psycopg2 to talk to Postgres.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash nimbusfs

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=nimbusfs:nimbusfs . .

USER nimbusfs

EXPOSE 8000

# Liveness, deliberately (see app/schemas/health.py's LivenessCheckResponse
# docstring for why this must NOT be /health or /ready: those touch
# DB/Redis/GCS, and Docker's HEALTHCHECK restarting a container because a
# downstream dependency is briefly unavailable would make an outage worse,
# not better).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/live || exit 1

# Multiple workers for production throughput; overridden by compose for dev.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
