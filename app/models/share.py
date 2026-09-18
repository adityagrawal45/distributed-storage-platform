"""
Share ORM model (Phase 13) — a time-boxed, revocable bearer credential
granting a specific `Permission` on one resource, without requiring the
holder to be a NimbusFS user at all.

Design decisions
----------------
- **Only `token_hash` (SHA-256) is stored, never the raw token.** The
  raw, high-entropy token (`SHARE_TOKEN_BYTES` random bytes, URL-safe
  base64 — see `ShareService.create`) is returned to the CREATOR
  exactly once, at creation time, and never persisted or logged in
  full anywhere in this codebase (Phase 13 §16). This is the same
  "store a hash, not the secret" principle `User.hashed_password`
  already applies to credentials — a share token is a bearer
  credential, so it gets the same treatment, not a lighter one just
  because it is not literally a login password. Redeeming a share
  hashes the presented token and looks up `token_hash` — indistinguishable
  in cost from a normal indexed lookup, unlike a `bcrypt`-style
  intentionally-slow hash (which would make every download through a
  share pay a deliberate throttling cost); the token's own 256 bits of
  entropy is what makes brute-forcing it infeasible, not the hash
  function's slowness.
- **`resource_type`/`resource_id` bare UUID**, same reasoning as
  `ResourcePermission` — one table, two possible targets, no FK to a
  single table possible.
- **`revoked_at` (nullable timestamp), not a boolean.** Recording WHEN
  a share was revoked is what `docs/sharing.md`'s audit trail and "was
  this access before or after revocation" investigations need — a bare
  `is_revoked` flag would lose that.
- **`password_hash` uses the same `bcrypt` helper as `User.hashed_password`**
  (`app/core/security/password.py`) — a share's optional password is a
  real secondary secret (Phase 13 §15/§16), not a token, so it gets the
  slow, salted hash a password deserves, not the fast SHA-256 the
  bearer token itself uses.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Permission, ResourceType
from app.database.session import Base


class Share(Base):
    __tablename__ = "shares"
    __table_args__ = (
        Index("ix_shares_token_hash", "token_hash", unique=True),
        Index("ix_shares_resource", "resource_type", "resource_id"),
        Index("ix_shares_organization_id", "organization_id"),
        Index("ix_shares_expires_at", "expires_at"),
        Index("ix_shares_created_by", "created_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    # Same native Postgres enum TYPE as ResourcePermission's identically-
    # named/configured columns (`name=`/`values_callable` must match
    # exactly) — SQLAlchemy reuses rather than re-creates a Postgres
    # enum type it already emitted DDL for under the same name.
    resource_type: Mapped[ResourceType] = mapped_column(
        SAEnum(
            ResourceType,
            name="resource_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    permission: Mapped[Permission] = mapped_column(
        SAEnum(
            Permission,
            name="permission",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    max_downloads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Share id={self.id} resource={self.resource_type}:{self.resource_id} revoked={self.revoked_at is not None}>"
