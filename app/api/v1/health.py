from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from app.api.dependencies import get_redis_client
from app.core.config import settings
from app.utils.response import success_response
import logging
from datetime import datetime, timezone

router = APIRouter(prefix="/health", tags=["Health"])
logger = logging.getLogger(__name__)


@router.get("")
async def health_check(
    db: AsyncSession = Depends(get_db),
    redis_client: Redis = Depends(get_redis_client),
):
    """Health check endpoint verifying DB and Redis connectivity."""
    status = {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }

    # Check Database
    try:
        await db.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        status["checks"]["database"] = "failed"
        status["status"] = "unhealthy"

    # Check Redis
    try:
        await redis_client.ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        status["checks"]["redis"] = "failed"
        status["status"] = "unhealthy"

    # Overall health
    if status["status"] != "healthy":
        # Return 503 if unhealthy
        from fastapi import Response
        response = success_response(
            message="Health check failed",
            data=status,
        )
        return Response(content=response.body, status_code=503, media_type="application/json")
    return success_response(message="Service healthy", data=status)