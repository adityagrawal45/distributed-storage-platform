"""Health check route — used by Kubernetes liveness/readiness probes."""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.config import get_settings
from app.database.redis import check_redis_connection
from app.database.session import check_database_connection
from app.schemas.health import ComponentStatus, HealthCheckResponse
from app.schemas.response import APIResponse

router = APIRouter(tags=["Health"])
settings = get_settings()


@router.get(
    "/health",
    response_model=APIResponse[HealthCheckResponse],
    summary="Application health check",
)
async def health_check() -> APIResponse[HealthCheckResponse]:
    db_healthy = await check_database_connection()
    redis_healthy = await check_redis_connection()
    overall = "healthy" if (db_healthy and redis_healthy) else "degraded"

    data = HealthCheckResponse(
        status=overall,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT.value,
        database=ComponentStatus(status="healthy" if db_healthy else "unhealthy"),
        redis=ComponentStatus(status="healthy" if redis_healthy else "unhealthy"),
        timestamp=datetime.now(timezone.utc),
    )
    return APIResponse(message="Health check completed.", data=data)
