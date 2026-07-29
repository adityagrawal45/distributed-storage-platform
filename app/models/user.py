"""
User ORM model.

Design decisions:
- Primary key is a UUID (v4), not an auto-increment integer. This avoids
  leaking sequential user counts, is safe to generate client- or
  server-side, and merges cleanly across a future multi-region /
  multi-service architecture (no cross-shard PK collisions).
- `email` has a unique index — it's our natural login identifier and
  the most frequent lookup during authentication, so it must be both
  unique (constraint) and fast to query (index).
- `role` is a native Postgres ENUM (via SQLAlchemy `Enum`) rather than a
  free-text string, so invalid roles are rejected at the database level
  too, not just in application code.
- `is_active` / `is_verified` are separate booleans: `is_active` supports
  admin-driven account suspension; `is_verified` supports a future
  email-verification flow. Keeping them independent avoids overloading
  one flag with two meanings.
- `created_at` / `updated_at` use server-side defaults (`func.now()`,
  `onupdate=func.now()`) so timestamps are set by Postgres itself,
  guaranteeing consistency even if multiple app instances have slightly
  skewed clocks.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class UserRole(str, Enum):
    """Application-level roles. Extend here as authorization needs grow."""

    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            native_enum=True,
            # By default, SQLAlchemy's Enum type binds the Python enum
            # member's `.name` ("USER") rather than its `.value` ("user").
            # Our Postgres enum type only contains the lowercase `.value`
            # labels (created by the Alembic migration), so we must tell
            # SQLAlchemy explicitly to bind/read `.value` on both the way
            # in and the way out.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UserRole.USER,
        nullable=False,
        server_default=UserRole.USER.value,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email} role={self.role}>"
