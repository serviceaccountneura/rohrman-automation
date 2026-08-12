"""Pydantic models for FastAPI request/response schemas."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field


# ── OCR ──────────────────────────────────────────────────────────────────────


class OcrJobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class OcrJobResponse(BaseModel):
    job_id: str
    status: OcrJobStatus


class OcrJobResult(BaseModel):
    job_id: str
    status: OcrJobStatus
    result: dict[str, Any] | None = None
    error: str | None = None


# ── Tekion ───────────────────────────────────────────────────────────────────


class PoType(str, Enum):
    SUBLET = "SUBLET"
    MISCELLANEOUS = "MISCELLANEOUS"
    STOCK = "STOCK"


# ── Line items (per PO type) ──────────────────────────────────────────────────


class StockPartInput(BaseModel):
    part_number: str = Field(alias="partNumber")
    part_name: str = Field(alias="partName")
    qty: float = 1.0
    unit_price: float = Field(alias="unitPrice")
    brand_code: str = Field(alias="brandCode")

    model_config = {"populate_by_name": True}


class MiscLineItem(BaseModel):
    part_name: str = Field(alias="partName")
    qty: float = 1.0
    unit_price: float = Field(alias="unitPrice")
    # Optional — auto-resolved from vendor table + department fallback if omitted.
    gl_account: str | None = Field(default=None, alias="glAccount")

    model_config = {"populate_by_name": True}


class SubletLineItem(BaseModel):
    ro_number: str = Field(alias="roNo")
    job_number: str = Field(alias="jobNo")
    description: str
    opcode: str = "SUBLET"
    labor_amount: float = Field(default=0.0, alias="laborAmount")
    parts_amount: float = Field(default=0.0, alias="partsAmount")

    model_config = {"populate_by_name": True}


# ── Discriminated union: po_type selects the schema ───────────────────────────


class _CreatePoBase(BaseModel):
    dealership_name: str = Field(alias="dealershipName")
    vendor_name: str = Field(alias="vendorName")
    invoice_number: str = Field(alias="invoiceNumber")
    invoice_amount: float = Field(alias="invoiceAmount")
    sales_tax: float = Field(default=0.0, alias="salesTax")

    model_config = {"populate_by_name": True}


class CreateSubletPoRequest(_CreatePoBase):
    # po_type is the discriminator — no alias so it matches the JSON key directly.
    po_type: Literal["SUBLET"] = "SUBLET"
    line_items: list[SubletLineItem] = Field(default_factory=list, alias="lineItems")


class CreateMiscPoRequest(_CreatePoBase):
    po_type: Literal["MISCELLANEOUS"] = "MISCELLANEOUS"
    line_items: list[MiscLineItem] = Field(default_factory=list, alias="lineItems")


class CreateStockPoRequest(_CreatePoBase):
    """Vendor stock order — create + submit only, no pre-invoice."""
    po_type: Literal["STOCK"] = "STOCK"
    parts: list[StockPartInput] = Field(default_factory=list, alias="parts")


# FastAPI / Pydantic dispatches to the correct schema based on po_type.
CreatePoRequest = Annotated[
    Union[CreateSubletPoRequest, CreateMiscPoRequest, CreateStockPoRequest],
    Field(discriminator="po_type"),
]


class VendorCandidate(BaseModel):
    id: str
    name: str
    display_id: str = Field(alias="displayId")
    site_id: str = Field(default="", alias="siteId")
    phone: str = ""
    email: str = ""

    model_config = {"populate_by_name": True}


class CreatePoResponse(BaseModel):
    success: bool
    po_number: str | None = None
    po_id: int | None = None
    po_status: str | None = None
    invoice_id: str | None = None
    vendor_name: str | None = None
    error: str | None = None
    # When vendor could not be resolved from the mapping, these are set:
    needs_review: bool = False
    review_reason: str | None = None
    vendor_candidates: list[VendorCandidate] = Field(default_factory=list)


# ── Auth ──────────────────────────────────────────────────────────────────────


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class UserLogin(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1)


class UserRead(BaseModel):
    id: UUID
    email: str
    full_name: str | None = None
    role: str
    is_active: bool
    is_superuser: bool
    created_at: datetime


class UserListItem(BaseModel):
    id: UUID
    full_name: str | None
    email: str
    role: str
    status: str  # ACTIVE / INACTIVE


class UserListResponse(BaseModel):
    items: list[UserListItem]
    total: int


class UpdateUserStatusRequest(BaseModel):
    is_active: bool


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expires


class RefreshRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    message: str


# ── Invoice upload ────────────────────────────────────────────────────────────


class UploadUrlRequest(BaseModel):
    file_name: str = Field(alias="fileName")
    dealership_name: str | None = Field(default=None, alias="dealershipName")

    model_config = {"populate_by_name": True}


class UploadUrlResponse(BaseModel):
    upload_url: str = Field(alias="uploadUrl")
    s3_key: str = Field(alias="s3Key")
    file_name: str = Field(alias="fileName")

    model_config = {"populate_by_name": True}


# ── Dashboard ─────────────────────────────────────────────────────────────────


class DashboardSummary(BaseModel):
    total: int
    processed: int
    exceptions: int
    auto_resolved: int


class DocumentsByType(BaseModel):
    SUBLET: int = 0
    MISCELLANEOUS: int = 0
    STOCK: int = 0


class ExceptionItem(BaseModel):
    id: UUID
    vendor: str
    invoice_number: str
    po_number: str
    exception_type: str


class DashboardResponse(BaseModel):
    summary: DashboardSummary
    by_type: DocumentsByType
    recent_exceptions: list[ExceptionItem]


class DocumentItem(BaseModel):
    id: UUID
    file_name: str
    dealership_name: str
    vendor_name: str
    invoice_number: str
    ro_number: str
    po_number: str
    po_type: str
    status: str
    exception_type: str | None
    created_at: datetime
    processed_at: datetime | None


class DocumentListResponse(BaseModel):
    items: list[DocumentItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ExceptionListItem(BaseModel):
    id: UUID
    vendor_name: str
    invoice_number: str
    vin: str
    po_number: str
    ro_number: str
    exception_type: str | None
    severity: str | None
    detected_on: datetime
    document_type: str
    status: str


class ExceptionListResponse(BaseModel):
    items: list[ExceptionListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ExceptionTypeCount(BaseModel):
    exception_type: str
    count: int


class ExceptionAnalyticsResponse(BaseModel):
    total_exceptions: int
    critical: int
    medium: int
    low: int
    auto_resolved: int
    by_exception_type: list[ExceptionTypeCount]


class NotificationItem(BaseModel):
    id: UUID
    title: str
    message: str
    category: str
    severity: str
    is_read: bool
    document_id: UUID | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    total: int
    unread_count: int
    page: int
    page_size: int
    total_pages: int
