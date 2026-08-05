"""
Health / readiness / liveness routes (Phase 4).

Three distinct endpoints, each with a distinct probe semantics — see
`app/schemas/health.py` docstrings for the full rationale:

- `GET /health` — deep diagnostic check (DB + Redis + Storage), for
  humans/dashboards/monitoring, not on any hot path.
- `GET /ready`  — same dependency checks, but shaped for a load
  balancer / Kubernetes readiness probe: `ready: bool` + `503` when not
  ready, so "not ready" is a clear failed health check to the LB, not a
  `200` the LB has to inspect a JSON body to understand.
- `GET /live`   — no dependency checks at all; answers instantly.
  Existing purely to answer "is this process able to respond" — a slow
  DB should never cause an otherwise-fine process to be killed and
  restarted, which is what a liveness-probe failure triggers.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.core.server_info import ServerIdentity, get_server_identity
from app.database.gcs import check_storage_connection
from app.database.redis import check_redis_connection
from app.database.session import check_database_connection
from app.dependencies.providers import GCSClientDep
from app.schemas.health import ComponentStatus, HealthCheckResponse, LivenessResponse, ReadinessResponse, ServerInfo
from app.schemas.response import APIResponse

router = APIRouter(tags=["Health"])


def _server_info(identity: ServerIdentity) -> ServerInfo:
    return ServerInfo(**identity.model_dump())


async def _check_component(check_coro) -> ComponentStatus:
    """
    Times and normalizes one dependency check into a `ComponentStatus`.
    The retry policy itself lives in each `check_*_connection()`
    function (see app/database/{session,redis,gcs}.py) — this helper
    only wraps the (already-retried) outcome, it doesn't add its own.
    """
    start = time.perf_counter()
    healthy = await check_coro
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    return ComponentStatus(status="healthy" if healthy else "unhealthy", response_time_ms=elapsed_ms)


@router.get(
    "/health",
    response_model=APIResponse[HealthCheckResponse],
    summary="Deep application health check (DB, Redis, Storage)",
)
async def health_check(gcs_client: GCSClientDep) -> APIResponse[HealthCheckResponse]:
    start = time.perf_counter()

    database = await _check_component(check_database_connection())
    redis_status = await _check_component(check_redis_connection())
    storage = await _check_component(check_storage_connection(gcs_client))

    all_healthy = all(c.status == "healthy" for c in (database, redis_status, storage))
    any_healthy = any(c.status == "healthy" for c in (database, redis_status, storage))
    overall = "healthy" if all_healthy else ("degraded" if any_healthy else "unhealthy")

    data = HealthCheckResponse(
        status=overall,
        server=_server_info(get_server_identity()),
        database=database,
        redis=redis_status,
        storage=storage,
        timestamp=datetime.now(timezone.utc),
        response_time_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return APIResponse(message="Health check completed.", data=data)


@router.get(
    "/ready",
    summary="Readiness probe — can this replica accept traffic right now?",
)
async def readiness_check(gcs_client: GCSClientDep):
    start = time.perf_counter()

    database = await _check_component(check_database_connection())
    redis_status = await _check_component(check_redis_connection())
    storage = await _check_component(check_storage_connection(gcs_client))

    ready = all(c.status == "healthy" for c in (database, redis_status, storage))

    data = ReadinessResponse(
        ready=ready,
        server=_server_info(get_server_identity()),
        database=database,
        redis=redis_status,
        storage=storage,
        timestamp=datetime.now(timezone.utc),
        response_time_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    envelope = APIResponse(
        success=ready,
        message="Ready to serve traffic." if ready else "Not ready: one or more dependencies are unavailable.",
        data=data,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=envelope.model_dump(mode="json"),
    )


@router.get(
    "/live",
    response_model=APIResponse[LivenessResponse],
    summary="Liveness probe — is this process alive? (no dependency checks)",
)
async def liveness_check() -> APIResponse[LivenessResponse]:
    data = LivenessResponse(
        alive=True,
        server=_server_info(get_server_identity()),
        timestamp=datetime.now(timezone.utc),
    )
    return APIResponse(message="Process is alive.", data=data)
