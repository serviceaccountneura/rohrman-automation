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
    # The documents row tracking this extraction. Pass it to POST /api/tekion/po
    # so the PO result is recorded against the same row.
    document_id: UUID | None = None


class OcrFields(BaseModel):
    """Flat, predictable field names extracted from the raw OCR output.

    The raw `result` is nested and label-driven (vendor:{name},
    identifiers:[{label,value}], totals:[{label,value}]) because the model
    chooses its own field names. Use this instead of digging through it.
    """

    document_type: str = ""
    dealership_name: str = ""
    vendor_name: str = ""
    invoice_number: str = ""
    invoice_date: str = ""  # MM/DD/YYYY
    invoice_amount: float = 0.0
    sales_tax: float = 0.0
    ro_number: str = ""
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    needs_review: bool = False


class OcrJobResult(BaseModel):
    job_id: str
    status: OcrJobStatus
    result: dict[str, Any] | None = None
    # Same values as `result`, flattened. Prefer this in clients.
    fields: OcrFields | None = None
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
    # Optional: local file path or S3 key of the invoice PDF to attach to the
    # pre-invoice. An S3 key (as returned by /api/invoices/upload-url) is
    # downloaded server-side before being uploaded to Tekion.
    invoice_file_path: str | None = Field(default=None, alias="invoiceFilePath")
    # Optional: the documents row from POST /api/ocr/extract. When given, the PO
    # result is recorded against it so this flow is tracked like the pipeline's.
    document_id: UUID | None = Field(default=None, alias="documentId")

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
    invite_code: str | None = Field(default=None, max_length=32)


class CreateInviteRequest(BaseModel):
    role: str = Field(default="AP_CLERK", max_length=50)
    full_name: str | None = Field(default=None, max_length=255)


class InviteResponse(BaseModel):
    invite_code: str
    invite_url: str
    role: str
    full_name: str | None
    expires_at: datetime


class InviteValidateResponse(BaseModel):
    valid: bool
    role: str | None = None
    full_name: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


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


# ── Upload pipeline ───────────────────────────────────────────────────────────


class PipelineFolder(str, Enum):
    """The frontend folder the invoice was uploaded into.

    This selects the Tekion flow and is authoritative — OCR's own document_type
    is recorded for review but never changes the routing.
    """

    SUBLET = "SUBLET"
    MISCELLANEOUS = "MISCELLANEOUS"
    STOCK = "STOCK"
    OEM = "OEM"


class PipelineAcceptedResponse(BaseModel):
    """Returned immediately on upload, before OCR runs."""

    document_id: UUID = Field(alias="documentId")
    status: str
    folder: str
    file_name: str = Field(alias="fileName")

    model_config = {"populate_by_name": True}


class PipelineStatusResponse(BaseModel):
    document_id: UUID = Field(alias="documentId")
    status: str
    folder: str = ""
    file_name: str = Field(default="", alias="fileName")
    s3_key: str = Field(default="", alias="s3Key")
    dealership_name: str = Field(default="", alias="dealershipName")
    vendor_name: str = Field(default="", alias="vendorName")
    invoice_number: str = Field(default="", alias="invoiceNumber")
    ro_number: str = Field(default="", alias="roNumber")
    # Set when the folder was SUBLET / MISCELLANEOUS / STOCK.
    po_number: str = Field(default="", alias="poNumber")
    # Set when the folder was OEM (journal entry).
    transaction_id: str = Field(default="", alias="transactionId")
    transaction_number: str = Field(default="", alias="transactionNumber")
    journal_id: str = Field(default="", alias="journalId")
    # What OCR thought the document was — for review when it disagrees.
    ocr_document_type: str = Field(default="", alias="ocrDocumentType")
    exception_type: str | None = Field(default=None, alias="exceptionType")
    severity: str | None = None
    # Queue bookkeeping — how many times it has been tried and why it last failed.
    attempts: int = 0
    last_error: str = Field(default="", alias="lastError")
    created_at: datetime = Field(alias="createdAt")
    processed_at: datetime | None = Field(default=None, alias="processedAt")

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
    # OEM documents become journal entries rather than POs, but they are counted
    # here too so the dashboard shows every folder.
    OEM: int = 0


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
