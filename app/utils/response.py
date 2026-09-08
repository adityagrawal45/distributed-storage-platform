from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel


class APIResponse(BaseModel):
    success: bool
    message: str
    data: Any | None = None
    timestamp: str
    request_id: str | None = None
    error_code: str | None = None
    details: dict[str, Any] | None = None


def success_response(
    message: str = "Success",
    data: Any | None = None,
    request_id: str | None = None,
) -> dict:
    return {
        "success": True,
        "message": message,
        "data": data,
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": request_id,
    }


def error_response(
    message: str = "Error",
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict:
    return {
        "success": False,
        "message": message,
        "data": None,
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": request_id,
        "error_code": error_code,
        "details": details,
    }
