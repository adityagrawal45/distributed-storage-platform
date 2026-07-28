from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel


class APIResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Any] = None
    timestamp: str
    request_id: Optional[str] = None
    error_code: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def success_response(
    message: str = "Success",
    data: Optional[Any] = None,
    request_id: Optional[str] = None,
) -> dict:
    return {
        "success": True,
        "message": message,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
    }


def error_response(
    message: str = "Error",
    error_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> dict:
    return {
        "success": False,
        "message": message,
        "data": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "error_code": error_code,
        "details": details,
    }