"""SQLModel table definitions — auth + vendor mappings + Tekion sessions + documents + GL mappings."""
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
    role: str = Field(default="AP_CLERK", max_length=50, index=True)
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


class TekionSession(SQLModel, table=True):
    """Persisted Tekion API session — avoids re-logging in on every request.

    The client tries the saved session first; if Tekion returns 401/403,
    it re-logs in and updates this row.
    """

    __tablename__ = "tekion_sessions"

    # Singleton row — always id=1.
    id: int = Field(default=1, primary_key=True)
    cookies: str = Field(default="", max_length=10000)
    api_token: str = Field(default="", max_length=1000)
    user_id: str = Field(default="", max_length=100)
    dealer_id: str = Field(default="", max_length=20)
    site_id: str = Field(default="", max_length=30)
    dealers_json: str = Field(default="[]", max_length=10000)
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime = Field(default_factory=_utcnow)


class Document(SQLModel, table=True):
    """Tracks each document (invoice/PO/parts ticket) through the automation pipeline.

    Powers the dashboard stats and exception queue.
    """

    __tablename__ = "documents"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    file_name: str = Field(default="", max_length=500)
    dealership_name: str = Field(default="", max_length=255, index=True)
    vendor_name: str = Field(default="", max_length=255, index=True)
    invoice_number: str = Field(default="", max_length=100)
    vin: str = Field(default="", max_length=20)
    ro_number: str = Field(default="", max_length=100)
    po_number: str = Field(default="", max_length=100)
    po_type: str = Field(default="", max_length=20, index=True)  # SUBLET, MISCELLANEOUS, STOCK
    # AP_INVOICE, PO, PARTS_TICKET, JOURNAL, GL_REPORT, STATEMENT, REPAIR_ORDER, MANUFACTURER_INVOICE
    document_type: str = Field(default="", max_length=30, index=True)
    # PENDING, PROCESSED, EXCEPTION, AUTO_RESOLVED
    status: str = Field(default="PENDING", max_length=20, index=True)
    # VENDOR_NOT_FOUND, PO_MISMATCH, AMOUNT_MISMATCH, LOW_OCR_CONFIDENCE, etc.
    exception_type: str | None = Field(default=None, max_length=100)
    # HIGH, MEDIUM, LOW
    severity: str | None = Field(default=None, max_length=10, index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    processed_at: datetime | None = Field(default=None)


class GlVendorMapping(SQLModel, table=True):
    """Master Miscellaneous Vendor-to-GL lookup table.

    Applies across all dealerships when generating a Miscellaneous PO.
    If a vendor is not in this table, the department fallback is used.
    """

    __tablename__ = "gl_vendor_mappings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Normalized vendor name (uppercase, stripped) for lookups.
    vendor_name: str = Field(index=True, unique=True, max_length=255)
    gl_account: str = Field(max_length=10)
    description: str = Field(default="", max_length=255)
    created_at: datetime = Field(default_factory=_utcnow)


class TekionGlAccount(SQLModel, table=True):
    """Cached GL accounts per dealership, fetched from Tekion.

    Used by the misc PO GL resolver to give the LLM real account names
    to match against line item descriptions.
    """

    __tablename__ = "tekion_gl_accounts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    dealer_id: str = Field(index=True, max_length=20)
    # Full Tekion GL account ID, e.g. "1708_7473"
    account_id: str = Field(max_length=50)
    # Account number only, e.g. "7473"
    account_number: str = Field(max_length=20)
    # Human-readable name, e.g. "OFF SUPPLY & EXP - SRV"
    account_name: str = Field(max_length=255)
    # e.g. "OPERATING_EXPENSE", "ASSET", "LIABILITY"
    account_type: str = Field(default="", max_length=50)
    # e.g. "SERVICE", "PARTS", "VEHICLE_SALES"
    department_type: str = Field(default="", max_length=50)
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)

    __table_args__ = (
        UniqueConstraint("dealer_id", "account_id", name="uq_tekion_gl_dealer_account"),
    )


class Notification(SQLModel, table=True):
    """User notifications — exceptions, processing events, system alerts."""

    __tablename__ = "notifications"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    title: str = Field(max_length=255)
    message: str = Field(default="", max_length=1000)
    # EXCEPTION, PO_CREATED, PROCESSED, SYSTEM, etc.
    category: str = Field(default="SYSTEM", max_length=50, index=True)
    # INFO, WARNING, ERROR
    severity: str = Field(default="INFO", max_length=10)
    is_read: bool = Field(default=False, index=True)
    # Optional link to a document
    document_id: UUID | None = Field(default=None, foreign_key="documents.id")
    created_at: datetime = Field(default_factory=_utcnow, index=True)
