"""
Health / Readiness / Liveness routes.

Three distinct probes, three distinct jobs (see app/schemas/health.py's
docstrings for the full reasoning on each):
- `GET /health`  — full dependency status, for dashboards/alerting/humans.
- `GET /ready`   — "route traffic to me?" for the load balancer / k8s
  readiness probe. Fails during startup and during graceful shutdown.
- `GET /live`    — "is the process itself alive?" for the k8s liveness
  probe. Checks nothing external, so a dependency outage never triggers
  a pointless container restart.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.server_identity import SERVER_IDENTITY
from app.database.gcs import check_storage_connection
from app.database.redis import check_redis_connection
from app.database.session import check_database_connection
from app.dependencies.providers import GCSClientDep
from app.schemas.health import (
    ComponentStatus,
    HealthCheckResponse,
    LivenessCheckResponse,
    ReadinessCheckResponse,
    ServerInfo,
)
from app.schemas.response import APIResponse

router = APIRouter(tags=["Health"])
settings = get_settings()


def _server_info() -> ServerInfo:
    return ServerInfo(**SERVER_IDENTITY.as_dict())


@router.get(
    "/health",
    response_model=APIResponse[HealthCheckResponse],
    summary="Full application health check (dependencies, server identity, latency)",
)
async def health_check(gcs_client: GCSClientDep) -> APIResponse[HealthCheckResponse]:
    start = time.perf_counter()
    db_healthy, db_latency = await check_database_connection()
    redis_healthy, redis_latency = await check_redis_connection()
    storage_healthy, storage_latency = await check_storage_connection(gcs_client)

    overall = "healthy" if (db_healthy and redis_healthy and storage_healthy) else "degraded"

    data = HealthCheckResponse(
        status=overall,
        server=_server_info(),
        database=ComponentStatus(status="healthy" if db_healthy else "unhealthy", latency_ms=db_latency),
        redis=ComponentStatus(status="healthy" if redis_healthy else "unhealthy", latency_ms=redis_latency),
        storage=ComponentStatus(status="healthy" if storage_healthy else "unhealthy", latency_ms=storage_latency),
        response_time_ms=round((time.perf_counter() - start) * 1000, 2),
        timestamp=datetime.now(timezone.utc),
    )
    return APIResponse(message="Health check completed.", data=data)


@router.get(
    "/ready",
    response_model=APIResponse[ReadinessCheckResponse],
    summary="Readiness probe: should the load balancer send this instance traffic?",
)
async def readiness_check(request: Request) -> JSONResponse:
    start = time.perf_counter()

    # `app.state.ready` is flipped False during startup (before dependency
    # checks pass) and during graceful shutdown (see app/main.py's
    # lifespan) — an instance that's draining must stop receiving new
    # traffic immediately, even if its DB/Redis connections still work.
    accepting_traffic = getattr(request.app.state, "ready", False)

    db_healthy, db_latency = await check_database_connection()
    redis_healthy, redis_latency = await check_redis_connection()

    ready = accepting_traffic and db_healthy and redis_healthy

    data = ReadinessCheckResponse(
        ready=ready,
        server=_server_info(),
        database=ComponentStatus(status="healthy" if db_healthy else "unhealthy", latency_ms=db_latency),
        redis=ComponentStatus(status="healthy" if redis_healthy else "unhealthy", latency_ms=redis_latency),
        response_time_ms=round((time.perf_counter() - start) * 1000, 2),
        timestamp=datetime.now(timezone.utc),
    )
    body = APIResponse(
        success=ready,
        message="Ready to accept traffic." if ready else "Not ready to accept traffic.",
        data=data,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(mode="json"),
    )


@router.get(
    "/live",
    response_model=APIResponse[LivenessCheckResponse],
    summary="Liveness probe: is this process still able to serve requests?",
)
async def liveness_check() -> APIResponse[LivenessCheckResponse]:
    data = LivenessCheckResponse(
        alive=True,
        instance_id=SERVER_IDENTITY.instance_id,
        timestamp=datetime.now(timezone.utc),
    )
    return APIResponse(message="Alive.", data=data)
