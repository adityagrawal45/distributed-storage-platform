"""
Standard API response envelope.

Every endpoint returns this shape so API consumers can write one generic
response parser instead of special-casing each endpoint. Generic typing
(`APIResponse[T]`) lets each route declare its precise `data` shape for
accurate OpenAPI schema generation while reusing this single wrapper.
"""

import uuid
from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str


class APIResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "Request completed successfully"
    data: T | None = None
    errors: list[ErrorDetail] | list[dict] | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    model_config = {"json_schema_extra": {"example": {
        "success": True,
        "message": "Request completed successfully",
        "data": {},
        "errors": None,
        "timestamp": "2026-07-29T10:00:00Z",
        "request_id": "b3b3b3b3-1234-4567-8901-abcdefabcdef",
    }}}
