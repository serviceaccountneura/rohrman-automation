"""SQLModel table definitions for auth (users + refresh tokens)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    hashed_password: str
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    # The jti (JWT ID) of the refresh token — used to look up + revoke.
    jti: str = Field(index=True, unique=True, max_length=64)
    # SHA-256 of the token so we never store the raw refresh token.
    token_hash: str = Field(index=True, max_length=128)
    expires_at: datetime
    revoked: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
