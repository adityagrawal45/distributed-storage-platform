import redis.asyncio as redis
from redis.asyncio import ConnectionPool
from app.core.config import settings

redis_pool: ConnectionPool | None = None


async def init_redis_pool():
    global redis_pool
    if redis_pool is None:
        redis_pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            decode_responses=True,
        )
    return redis_pool


async def get_redis() -> redis.Redis:
    if redis_pool is None:
        await init_redis_pool()
    return redis.Redis(connection_pool=redis_pool)