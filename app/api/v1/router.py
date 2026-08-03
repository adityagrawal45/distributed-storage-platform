"""Aggregates all v1 routers into a single APIRouter mounted by main.py."""

from fastapi import APIRouter

from app.api.v1.auth.routes import router as auth_router
from app.api.v1.files.routes import router as files_router
from app.api.v1.folders.routes import router as folders_router
from app.api.v1.health.routes import router as health_router
from app.api.v1.metadata.routes import router as metadata_router
from app.api.v1.trash.routes import router as trash_router
from app.api.v1.users.routes import router as users_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(folders_router)
api_router.include_router(metadata_router)
api_router.include_router(files_router)
api_router.include_router(trash_router)