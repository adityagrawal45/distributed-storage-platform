# syntax=docker/dockerfile:1
#
# Phase 5: this is now the SINGLE canonical Dockerfile — used by both
# `docker-compose.yml` (local dev, with `command:` overriding CMD for
# --reload) and every GKE Deployment image (k8s/07-deployment.yaml,
# built via Cloud Build / Artifact Registry — see k8s/README.md "CI/CD
# Preparation"). One image definition for every environment is the
# actual mechanism behind the "immutable infrastructure" claim in
# k8s/README.md: staging and production run bit-for-bit the same image,
# never a re-build with different flags.

# ---------------------------------------------------------------------
# Stage 1: builder — installs dependencies into an isolated venv layer
# ---------------------------------------------------------------------
# Multi-stage build, optimization #1: the compiler toolchain (gcc,
# libpq-dev headers) needed to build wheels for psycopg2/bcrypt never
# ends up in the final image — only the compiled venv does. This alone
# cuts the shipped image by the size of a full C toolchain (hundreds of
# MB) and, just as importantly, removes `gcc`/build headers as an
# attack surface: an attacker who gets code execution in the runtime
# container cannot compile anything, because there's nothing to compile
# with.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Optimization #2: `requirements.txt` is copied and installed BEFORE
# the application source (below, in the runtime stage). Docker layer
# caching means this expensive `pip install` layer is only invalidated
# — and re-run — when dependencies actually change, not on every code
# change. In CI, this is the difference between a ~2s rebuild and a
# ~60s one on every commit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------
# Stage 2: runtime — slim final image, no build toolchain
# ---------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Baked in at build time via `--build-arg` (see k8s/README.md "CI/CD
# Preparation" for the `docker build --build-arg GIT_COMMIT=$(git
# rev-parse HEAD) --build-arg BUILD_VERSION=$(git describe --tags)`
# invocation) — NOT computed at container runtime. This is what lets
# `app/core/config/settings.py`'s BUILD_VERSION/GIT_COMMIT fields (and
# therefore every /health response and every log line — Phase 4) prove
# exactly which build produced them, independent of whatever image tag
# happens to be deployed.
ARG GIT_COMMIT=unknown
ARG BUILD_VERSION=dev-local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    GIT_COMMIT=${GIT_COMMIT} \
    BUILD_VERSION=${BUILD_VERSION}

# Optimization #3: only the runtime shared library (`libpq5`) is
# installed here, not the `-dev` headers the builder stage needed to
# COMPILE against libpq — same library, a fraction of the size.
# `curl` is kept only for the container-level HEALTHCHECK below; it's
# the one deliberate exception to "minimal attack surface" and is
# small/well-audited enough to accept.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    # Security optimization #4: a dedicated, unprivileged user+group
    # with a fixed numeric UID/GID (1000:1000) — fixed specifically so
    # it matches `runAsUser`/`runAsGroup`/`fsGroup: 1000` in
    # k8s/07-deployment.yaml exactly; Kubernetes enforces
    # `runAsNonRoot`/UID pinning at the Pod spec level, but the image
    # itself must actually contain that user for `USER nimbusfs` below
    # to resolve, and for GKE's Pod Security "restricted" admission
    # profile (k8s/00-namespace.yaml) to allow this Pod to be created
    # at all — a container that still defaults to root here would be
    # rejected outright by that namespace, not just discouraged.
    && groupadd --gid 1000 nimbusfs \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin nimbusfs

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=nimbusfs:nimbusfs . .

# Security optimization #5: switch to the unprivileged user BEFORE the
# final CMD — everything from this line down (and the running process
# itself) executes as UID 1000, never root. Combined with
# k8s/07-deployment.yaml's `readOnlyRootFilesystem: true` and
# `allowPrivilegeEscalation: false`, a compromised request handler
# cannot write to the image filesystem, escalate privileges, or bind a
# privileged port even if it achieves code execution.
USER nimbusfs

EXPOSE 8000

# Container-level HEALTHCHECK — Docker/Compose's own liveness signal,
# independent of (but philosophically identical to) the Kubernetes
# livenessProbe (k8s/07-deployment.yaml). Deliberately hits `/live`
# (Phase 4: zero dependency checks, constant-time) rather than
# `/health` or `/ready` — same reasoning as the k8s liveness probe:
# this HEALTHCHECK exists to answer "is the process itself still
# responsive", and a slow/unreachable Postgres should never cause
# Docker to report this container unhealthy and restart it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/live || exit 1

# Optimization #6: exec-form CMD (JSON array), not shell-form
# (`CMD uvicorn ...` as a bare string). Shell-form runs the process as
# a child of `/bin/sh -c`, which does NOT forward SIGTERM to uvicorn —
# a container stopped that way ignores graceful shutdown entirely and
# waits out the full grace period before SIGKILL. Exec-form makes
# uvicorn PID 1 (or the direct child Kubernetes signals), so SIGTERM
# reaches it immediately and app/main.py's lifespan shutdown hook
# (Phase 4: close DB pool, close Redis pool) actually gets to run.
#
# `--workers 4`: multiple uvicorn worker PROCESSES per container,
# distinct from and complementary to Kubernetes' Pod-level replication
# (07-deployment.yaml's `replicas: 3`) — this multiplies each Pod's
# request-handling concurrency across the vCPUs available up to that
# Pod's own `resources.limits.cpu` (500m => effectively CPU-
# constrained regardless of worker count, so `--workers 4` here trades
# a little memory for better tail latency under bursty concurrent
# I/O-bound requests, not for raw parallelism it doesn't have room to
# use). Overridden by `docker-compose.yml`'s `command:` in local dev
# for `--reload` + single-worker ergonomics.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
