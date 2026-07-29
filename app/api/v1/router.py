"""Aggregates all v1 routers into a single APIRouter mounted by main.py."""

from fastapi import APIRouter

from app.api.v1.auth.routes import router as auth_router
from app.api.v1.health.routes import router as health_router
from app.api.v1.users.routes import router as users_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
