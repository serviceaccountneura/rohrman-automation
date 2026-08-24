"""SQLModel table definitions — auth + vendor mappings + Tekion sessions + documents + GL mappings."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_plus_hours(hours: int) -> datetime:
    from datetime import timedelta

    return datetime.now(timezone.utc) + timedelta(hours=hours)


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
    # Where the uploaded file landed in S3, so the row points at the source doc.
    s3_key: str = Field(default="", max_length=1000)
    dealership_name: str = Field(default="", max_length=255, index=True)
    vendor_name: str = Field(default="", max_length=255, index=True)
    invoice_number: str = Field(default="", max_length=100)
    vin: str = Field(default="", max_length=20)
    ro_number: str = Field(default="", max_length=100)
    po_number: str = Field(default="", max_length=100)
    # SUBLET, MISCELLANEOUS, STOCK, OEM. Set from the upload folder, which is
    # authoritative: it selects the pipeline even when OCR disagrees.
    po_type: str = Field(default="", max_length=20, index=True)
    # AP_INVOICE, PO, PARTS_TICKET, JOURNAL, GL_REPORT, STATEMENT, REPAIR_ORDER, MANUFACTURER_INVOICE
    document_type: str = Field(default="", max_length=30, index=True)
    # What OCR actually detected. Kept alongside po_type so a folder/OCR
    # mismatch stays visible without blocking the run.
    ocr_document_type: str = Field(default="", max_length=100)
    # ── Journal entry results (OEM folder) ────────────────────────────────────
    # Tekion's transaction id / human-facing number / "{dealerId}_{journal}".
    transaction_id: str = Field(default="", max_length=50)
    transaction_number: str = Field(default="", max_length=50)
    journal_id: str = Field(default="", max_length=50)
    # QUEUED, PROCESSING, PROCESSED, EXCEPTION, DUPLICATE, AUTO_RESOLVED
    # (PENDING is retained for rows created before the queue existed.)
    #
    # DUPLICATE is a decision point, not a failure: OCR matched an invoice that
    # was already processed, so the run is held until someone confirms or
    # discards it. Nothing was sent to Tekion.
    status: str = Field(default="QUEUED", max_length=20, index=True)
    # VENDOR_NOT_FOUND, PO_MISMATCH, AMOUNT_MISMATCH, LOW_OCR_CONFIDENCE, etc.
    exception_type: str | None = Field(default=None, max_length=100)
    # HIGH, MEDIUM, LOW
    severity: str | None = Field(default=None, max_length=10, index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    processed_at: datetime | None = Field(default=None)

    # ── Queue bookkeeping ─────────────────────────────────────────────────────
    # This table doubles as the job queue: workers claim rows with
    # SELECT ... FOR UPDATE SKIP LOCKED, so status lives in one place and the
    # dashboard sees queue state for free.
    #
    # Where the uploaded file is on disk for the worker to read. Falls back to
    # downloading s3_key when the temp file is gone (e.g. after a restart).
    source_path: str = Field(default="", max_length=1000)
    # SHA-256 of the uploaded bytes — used to spot a re-upload of the same file.
    file_hash: str = Field(default="", max_length=64, index=True)
    attempts: int = Field(default=0)
    # Set while a worker holds the row; cleared when it finishes.
    locked_at: datetime | None = Field(default=None)
    locked_by: str = Field(default="", max_length=100)
    # Retry backoff — the row is invisible to claims until this passes.
    next_attempt_at: datetime | None = Field(default=None, index=True)
    last_error: str = Field(default="", max_length=1000)

    # ── Duplicate handling ────────────────────────────────────────────────────
    # The already-processed document this one repeats. Set alongside the
    # DUPLICATE status so the UI can show what it collides with.
    duplicate_of: UUID | None = Field(default=None, foreign_key="documents.id")
    # Skip the duplicate check for exactly one run. Set when a user confirms a
    # duplicate should be reprocessed anyway; cleared as soon as it is honoured,
    # so a later upload is still checked normally.
    duplicate_override: bool = Field(default=False)

    # ── Batch scans ───────────────────────────────────────────────────────────
    # Several invoices scanned into one file are split into one document each.
    # The parent keeps the original file and the SPLIT status; each child points
    # back here and owns the pages it was cut from. A child is never re-split.
    split_from: UUID | None = Field(default=None, foreign_key="documents.id")
    # Which pages of the parent this document is, e.g. "1-2" or "3". Empty for
    # anything that was not split out of a batch.
    page_range: str = Field(default="", max_length=20)


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


class InviteCode(SQLModel, table=True):
    """Single-use invite codes for user registration.

    Created by an admin, expires in 24 hours, consumed on signup.
    """

    __tablename__ = "invite_codes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(index=True, unique=True, max_length=32)
    # Role to assign when the invite is used
    role: str = Field(default="AP_CLERK", max_length=50)
    # Pre-filled name (optional — user can override on signup)
    full_name: str | None = Field(default=None, max_length=255)
    # Who created the invite
    created_by: UUID = Field(foreign_key="users.id")
    used: bool = Field(default=False, index=True)
    used_by: UUID | None = Field(default=None, foreign_key="users.id")
    used_at: datetime | None = Field(default=None)
    expires_at: datetime = Field(default_factory=lambda: _utcnow_plus_hours(24), index=True)
    created_at: datetime = Field(default_factory=_utcnow)
