"""
Password hashing utilities.

Design decisions:
- We use `passlib`'s bcrypt scheme rather than raw `bcrypt` calls because
  passlib handles algorithm versioning/migration (`CryptContext` can be
  extended with new schemes later without breaking old hashes).
- bcrypt automatically salts each hash, so no separate salt storage or
  management is required.
- We do not cap or silently truncate passwords ourselves; bcrypt has a
  72-byte input limit, which is enforced (and validated) at the schema
  layer instead of failing silently here.
"""

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    return _pwd_context.verify(plain_password, hashed_password)
