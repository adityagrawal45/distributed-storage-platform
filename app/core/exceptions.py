from typing import Any, Dict, Optional


class AppException(Exception):
    """Base application exception."""
    def __init__(
        self,
        message: str = "An error occurred",
        status_code: int = 500,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code or "internal_error"
        self.details = details or {}
        super().__init__(message)


class AuthenticationError(AppException):
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, status_code=401, error_code="authentication_error")


class AuthorizationError(AppException):
    def __init__(self, message: str = "Not authorized"):
        super().__init__(message, status_code=403, error_code="authorization_error")


class ValidationError(AppException):
    def __init__(self, message: str = "Validation error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=422, error_code="validation_error", details=details)


class NotFoundError(AppException):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404, error_code="not_found")


class ConflictError(AppException):
    def __init__(self, message: str = "Resource already exists"):
        super().__init__(message, status_code=409, error_code="conflict")