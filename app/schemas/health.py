"""Pydantic schemas for the health check endpoint."""

from datetime import datetime

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    status: str  # "healthy" | "unhealthy"


class HealthCheckResponse(BaseModel):
    status: str  # "healthy" | "degraded"
    version: str
    environment: str
    database: ComponentStatus
    redis: ComponentStatus
    timestamp: datetime
