"""
Domain-level exceptions.

Design decision: these exceptions carry no knowledge of HTTP status codes
or FastAPI. They represent things that went wrong in the domain/application
layer. The API layer (global exception handler) is solely responsible for
translating them into HTTP responses. This keeps services/repositories
framework-agnostic and testable in isolation.
"""


class NimbusFSException(Exception):
    """Base class for all application-specific exceptions."""

    def __init__(self, detail: str = "An application error occurred."):
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------
class AuthenticationException(NimbusFSException):
    """Raised when credentials are invalid or a token cannot be trusted."""

    def __init__(self, detail: str = "Authentication failed."):
        super().__init__(detail)


class InvalidCredentialsException(AuthenticationException):
    def __init__(self, detail: str = "Incorrect email or password."):
        super().__init__(detail)


class InvalidTokenException(AuthenticationException):
    def __init__(self, detail: str = "Invalid authentication token."):
        super().__init__(detail)


class TokenExpiredException(AuthenticationException):
    def __init__(self, detail: str = "Authentication token has expired."):
        super().__init__(detail)


class InactiveUserException(AuthenticationException):
    def __init__(self, detail: str = "User account is inactive."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------
class AuthorizationException(NimbusFSException):
    """Raised when an authenticated user lacks permission for an action."""

    def __init__(self, detail: str = "You do not have permission to perform this action."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Resource / Conflict
# ---------------------------------------------------------------------
class NotFoundException(NimbusFSException):
    def __init__(self, detail: str = "Resource not found."):
        super().__init__(detail)


class ConflictException(NimbusFSException):
    def __init__(self, detail: str = "Resource already exists."):
        super().__init__(detail)


class EmailAlreadyExistsException(ConflictException):
    def __init__(self, detail: str = "An account with this email already exists."):
        super().__init__(detail)


# ---------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------
class DatabaseConnectionException(NimbusFSException):
    def __init__(self, detail: str = "Database connection error."):
        super().__init__(detail)


class RedisConnectionException(NimbusFSException):
    def __init__(self, detail: str = "Redis connection error."):
        super().__init__(detail)
