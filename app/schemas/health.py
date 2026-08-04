"""Pydantic schemas for the health/readiness/liveness endpoints."""

from datetime import datetime

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    status: str  # "healthy" | "unhealthy"
    latency_ms: float | None = None


class ServerInfo(BaseModel):
    """Server identification block — see app/core/server_identity.py."""

    instance_id: str
    hostname: str
    process_id: int
    environment: str
    app_version: str
    build_version: str
    git_commit_sha: str
    started_at: datetime


class HealthCheckResponse(BaseModel):
    """`GET /health` — full dependency status, for dashboards/alerting."""

    status: str  # "healthy" | "degraded"
    server: ServerInfo
    database: ComponentStatus
    redis: ComponentStatus
    storage: ComponentStatus
    response_time_ms: float
    timestamp: datetime


class ReadinessCheckResponse(BaseModel):
    """
    `GET /ready` — for the load balancer / orchestrator's readiness probe.

    Distinct from `/health`: this answers ONE question ("should traffic
    be routed to me right now?"), not "how is every dependency doing."
    `ready=False` during startup (before dependencies are confirmed) and
    during shutdown (the instance is draining) even though the process
    itself is still alive — see app/main.py's lifespan.
    """

    ready: bool
    server: ServerInfo
    database: ComponentStatus
    redis: ComponentStatus
    response_time_ms: float
    timestamp: datetime


class LivenessCheckResponse(BaseModel):
    """
    `GET /live` — for the orchestrator's liveness probe.

    Deliberately checks NOTHING but "is this process able to answer HTTP
    requests at all." No DB/Redis/GCS calls here on purpose: a liveness
    probe answering "no" tells Kubernetes/Cloud Run to KILL and restart
    the container — which is the right response to a deadlocked event
    loop, and the catastrophically wrong response to "Postgres is
    temporarily down" (restarting every instance would not fix Postgres,
    and would drop every in-flight request for nothing). Dependency
    outages are `/ready`'s job, not `/live`'s.
    """

    alive: bool
    instance_id: str
    timestamp: datetime
