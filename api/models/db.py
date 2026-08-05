"""SQLModel table definitions — auth + vendor mappings."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import UniqueConstraint
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


class VendorMapping(SQLModel, table=True):
    """Maps (dealer_id, vendor_name) → Tekion vendorDisplayId.

    Seeded per-dealership so PO creation resolves the exact vendor
    instead of fuzzy-searching Tekion and taking the first hit.
    """

    __tablename__ = "vendor_mappings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    dealer_id: str = Field(index=True, max_length=20)
    # Normalized vendor name — uppercase, stripped. Used for lookups.
    vendor_name: str = Field(index=True, max_length=255)
    # Tekion vendorDisplayId, e.g. "1707_160"
    vendor_display_id: str = Field(max_length=100)
    created_at: datetime = Field(default_factory=_utcnow)

    __table_args__ = (
        UniqueConstraint("dealer_id", "vendor_name", name="uq_vendor_mapping_dealer_vendor"),
    )
