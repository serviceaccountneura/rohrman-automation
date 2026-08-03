"""Pydantic models for FastAPI request/response schemas."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
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


class LineItemInput(BaseModel):
    description: str
    qty: float = 1.0
    unit_price: float = Field(default=0.0, alias="unitPrice")
    total_price: float = Field(default=0.0, alias="totalPrice")

    model_config = {"populate_by_name": True}


class SubletItemInput(BaseModel):
    description: str
    labor_amount: float = Field(default=0.0, alias="laborAmount")
    parts_amount: float = Field(default=0.0, alias="partsAmount")

    model_config = {"populate_by_name": True}


class CreateSubletPoRequest(BaseModel):
    dealership_name: str = Field(alias="dealershipName")
    vendor_name: str = Field(alias="vendorName")
    control_number: str = Field(alias="controlNumber")
    invoice_number: str = Field(alias="invoiceNumber")
    invoice_amount: float = Field(alias="invoiceAmount")
    sales_tax: float = Field(default=0.0, alias="salesTax")
    gl_account: str = Field(default="2460", alias="glAccount")
    job_type: str | None = Field(default=None, alias="jobType")
    opcode: str = "SUBLET"
    category: str = "MISCELLANEOUS"
    line_items: list[SubletItemInput] = Field(default_factory=list, alias="lineItems")

    model_config = {"populate_by_name": True}


class CreateMiscPoRequest(BaseModel):
    dealership_name: str = Field(alias="dealershipName")
    vendor_name: str = Field(alias="vendorName")
    invoice_number: str = Field(alias="invoiceNumber")
    invoice_amount: float = Field(alias="invoiceAmount")
    sales_tax: float = Field(default=0.0, alias="salesTax")
    gl_account: str = Field(default="0021", alias="glAccount")
    line_items: list[LineItemInput] = Field(default_factory=list, alias="lineItems")

    model_config = {"populate_by_name": True}


class CreatePoResponse(BaseModel):
    success: bool
    po_number: str | None = None
    po_id: int | None = None
    po_status: str | None = None
    invoice_id: str | None = None
    vendor_name: str | None = None
    error: str | None = None


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
    is_active: bool
    is_superuser: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expires


class RefreshRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    message: str
