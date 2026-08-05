"""Pydantic schemas for the health/readiness/liveness endpoints (Phase 4)."""

from datetime import datetime

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    status: str  # "healthy" | "unhealthy"
    response_time_ms: float | None = None


class ServerInfo(BaseModel):
    """See app/core/server_info.py — one server's self-reported identity."""

    instance_id: str
    hostname: str
    process_id: int
    app_version: str
    build_version: str
    git_commit: str
    environment: str


class HealthCheckResponse(BaseModel):
    """`GET /health` — deep check, includes every dependency. Not for hot-path probing."""

    status: str  # "healthy" | "degraded" | "unhealthy"
    server: ServerInfo
    database: ComponentStatus
    redis: ComponentStatus
    storage: ComponentStatus
    timestamp: datetime
    response_time_ms: float


class ReadinessResponse(BaseModel):
    """
    `GET /ready` — "can this replica accept traffic right now?" Checks
    the same dependencies as `/health` (a replica that can't reach its
    database isn't ready), which is exactly what a Kubernetes readiness
    probe / load balancer health check needs to decide whether to route
    traffic to this replica.
    """

    ready: bool
    server: ServerInfo
    database: ComponentStatus
    redis: ComponentStatus
    storage: ComponentStatus
    timestamp: datetime
    response_time_ms: float


class LivenessResponse(BaseModel):
    """
    `GET /live` — "is this process alive and able to respond at all?"
    Deliberately checks NOTHING external (no DB/Redis/GCS calls) — a
    liveness probe exists to answer "should this process be killed and
    restarted", and a slow downstream dependency is a readiness problem,
    not a reason to kill an otherwise-healthy process. Answers in
    constant time regardless of infrastructure state.
    """

    alive: bool
    server: ServerInfo
    timestamp: datetime
