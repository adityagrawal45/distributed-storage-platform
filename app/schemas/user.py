"""
Pydantic v2 schemas for User input/output.

Design decisions:
- `UserBase` holds fields shared between create/read to avoid duplication.
- `UserCreate` enforces password strength at the API boundary (fail fast,
  before touching the DB or hashing anything).
- `UserRead` is the ONLY user-facing shape ever returned by the API — it
  deliberately excludes `hashed_password`. There is no `UserORM ->
  dict -> JSON` shortcut anywhere in the codebase that could leak it.
- `from_attributes=True` allows constructing these schemas directly from
  SQLAlchemy ORM instances (`UserRead.model_validate(user)`).
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import UserRole

_PASSWORD_MIN_LENGTH = 8
_PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s]).{8,}$")


class UserBase(BaseModel):
    first_name: str = Field(min_length=1, max_length=100, examples=["Ada"])
    last_name: str = Field(min_length=1, max_length=100, examples=["Lovelace"])
    email: EmailStr = Field(examples=["ada@nimbusfs.io"])


class UserCreate(UserBase):
    password: str = Field(min_length=_PASSWORD_MIN_LENGTH, max_length=72, examples=["StrongP@ssw0rd"])

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        """
        Require at least one lowercase, one uppercase, one digit, and one
        special character. Enforced here — at the schema boundary — so
        weak passwords never reach the service/hashing layer.
        """
        if not _PASSWORD_PATTERN.match(value):
            raise ValueError(
                "Password must contain at least one uppercase letter, one "
                "lowercase letter, one digit, and one special character."
            )
        return value


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime
