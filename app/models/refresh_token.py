"""
RefreshToken ORM model.

Design decision: we do NOT store the raw JWT refresh token in the
database. We store its `jti` (JWT ID) plus metadata. This lets us:
  1. Revoke a specific refresh token (e.g. on logout) by marking its
     `jti` revoked, without needing to store/compare full token strings.
  2. Implement refresh-token rotation: when a refresh token is used, we
     revoke its `jti` and issue a new one. If a revoked `jti` is ever
     presented again, we know the token was replayed (e.g. stolen) and
     can react (Phase 1: reject; future phase: revoke the whole session
     family).
  3. Avoid storing sensitive secrets (the signed token itself) at rest.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, index=True, nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RefreshToken jti={self.jti} user_id={self.user_id} revoked={self.revoked}>"
