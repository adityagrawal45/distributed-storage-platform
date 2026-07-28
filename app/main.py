from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging
import uuid
import time
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import AppException
from app.api.v1 import auth, health, users
from app.infrastructure.database import engine
from app.infrastructure.redis_client import init_redis_pool
from app.domain.models import Base
from app.utils.response import error_response

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up application...")
    # Initialize Redis pool
    await init_redis_pool()
    # Create tables if not exist (in production use migrations)
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.create_all)
    logger.info("Database and Redis connections established")
    yield
    # Shutdown
    logger.info("Shutting down...")
    await engine.dispose()
    # Redis pool cleanup will happen automatically


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted Host (optional)
# app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# Middleware for request-id and logging
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(process_time)
    logger.info(
        f"Request {request.method} {request.url.path}",
        extra={"request_id": request_id, "status": response.status_code, "duration": process_time}
    )
    return response


# Global exception handlers
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    logger.error(f"AppException: {exc.message}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=exc.message,
            error_code=exc.error_code,
            details=exc.details,
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning(f"HTTPException: {exc.detail}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=exc.detail,
            error_code="http_error",
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    details = {}
    for error in errors:
        field = ".".join(str(loc) for loc in error["loc"])
        details[field] = error["msg"]
    logger.warning(f"Validation error: {details}", extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_response(
            message="Validation error",
            error_code="validation_error",
            details=details,
        ),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response(
            message="Internal server error",
            error_code="internal_error",
        ),
    )


# Include routers
app.include_router(auth.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")

# Root endpoint
@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}"}