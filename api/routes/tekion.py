"""Tekion routes — unified PO creation + vendor mapping management.

POST /api/tekion/po        — create sublet, misc, or stock PO (discriminated by po_type)
GET  /api/tekion/vendors/{dealer_id}  — list vendor mappings for a dealer
POST /api/tekion/vendors   — add/update a vendor mapping (after human review)
"""
from __future__ import annotations

import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import VendorMapping
from api.models.schemas import (
    CreateMiscPoRequest,
    CreatePoRequest,
    CreatePoResponse,
    CreateStockPoRequest,
    CreateSubletPoRequest,
    VendorCandidate,
)
from api.services.tekion_client import TekionApiClient
from api.services.vendor_service import _normalize, resolve_vendor

router = APIRouter(prefix="/api/tekion", tags=["tekion"])

# Shared client — hydrated from DB session, re-logs in lazily on 401.
_client: TekionApiClient | None = None
_client_lock = threading.Lock()


def get_client(db_session: Session) -> TekionApiClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = TekionApiClient(db_session=db_session)
            # Try to restore the saved session before logging in.
            if not _client.load_session():
                _client.login()
        else:
            # Keep the DB session reference current (each request has its own).
            _client._db = db_session
        return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        _client = None


def _resolve_dealer(client: TekionApiClient, dealership_name: str) -> str:
    dealer_id = client.find_dealer_by_name(dealership_name)
    if dealer_id:
        client.switch_dealer(dealer_id)
        return dealer_id
    raise HTTPException(
        status_code=400,
        detail=f"Could not match dealership '{dealership_name}' to any dealer",
    )


def _resolve_vendor(
    client: TekionApiClient,
    dealer_id: str,
    vendor_name: str,
    session: Session,
) -> dict:
    result = resolve_vendor(dealer_id, vendor_name, session, client)
    if result["resolved"]:
        return result["vendor"]

    candidates = [
        VendorCandidate(
            id=c["id"],
            name=c["name"],
            displayId=c["displayId"],
            siteId=c.get("siteId", ""),
            phone=c.get("phone", ""),
            email=c.get("email", ""),
        )
        for c in result.get("candidates", [])
    ]
    reason = result.get("reason", "not_in_mapping")
    detail = {
        "message": (
            f"Vendor '{vendor_name}' is not in the mapping for dealer {dealer_id}."
            if reason == "not_in_mapping"
            else f"Vendor displayId '{result.get('vendor_display_id')}' from mapping "
            f"was not found in Tekion (stale mapping)."
        ),
        "reason": reason,
        "dealer_id": dealer_id,
        "vendor_name": vendor_name,
        "candidates": [c.model_dump(by_alias=True) for c in candidates],
    }
    raise HTTPException(status_code=422, detail=detail)


# ── Unified PO creation ───────────────────────────────────────────────────────


@router.post("/po", response_model=CreatePoResponse)
def create_po(
    req: CreatePoRequest,
    session: Annotated[Session, Depends(get_session)],
) -> CreatePoResponse:
    if isinstance(req, CreateSubletPoRequest):
        return _create_sublet_po(req, session)
    if isinstance(req, CreateStockPoRequest):
        return _create_stock_po(req, session)
    return _create_misc_po(req, session)


def _create_sublet_po(
    req: CreateSubletPoRequest,
    session: Session,
) -> CreatePoResponse:
    try:
        client = get_client(session)
        dealer_id = _resolve_dealer(client, req.dealership_name)
        vendor = _resolve_vendor(client, dealer_id, req.vendor_name, session)

        # Build sublet items — each line item has its own RO + job.
        items: list[dict] = []
        for li in req.line_items:
            ros = client.search_ro(li.ro_number)
            if not ros:
                raise HTTPException(
                    status_code=404,
                    detail=f"No RO found for '{li.ro_number}'",
                )
            ro = ros[0]

            jobs = client.get_ro_jobs(ro["id"])
            if not jobs:
                raise HTTPException(
                    status_code=404,
                    detail=f"No jobs found on RO {li.ro_number}",
                )

            job = jobs[0]
            matched = next((j for j in jobs if j["jobNumber"] == li.job_number), None)
            if matched:
                job = matched
            elif li.job_number:
                raise HTTPException(
                    status_code=404,
                    detail=f"Job '{li.job_number}' not found on RO {li.ro_number}",
                )

            items.append(
                {
                    "description": li.description,
                    "jobId": job["id"],
                    "referenceId": ro["id"],
                    "referenceNumber": ro["roNumber"],
                    "opcode": li.opcode,
                    "category": "MISCELLANEOUS",
                    "laborAmount": li.labor_amount,
                    "partsAmount": li.parts_amount,
                }
            )

        # If no line items, we still need an RO — require at least one.
        if not items:
            raise HTTPException(
                status_code=422,
                detail="Sublet PO requires at least one line item with an RO number",
            )

        po = client.create_sublet_po(
            vendor_id=int(vendor["id"]),
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            vendor_site_id=vendor["siteId"],
            vendor_phone=vendor["phone"],
            vendor_email=vendor["email"],
            items=items,
        )

        # Pre-invoice — GL 2460 for sublet, AP GL 3002.
        gl_account_id = f"{dealer_id}_2460"
        ap_gl_account_id = f"{dealer_id}_3002"
        ref_text = req.line_items[0].ro_number

        result = client.pre_invoice(
            vendor_id=vendor["id"],
            vendor_site_id=vendor["siteId"],
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            dealer_id=dealer_id,
            po_id=po["poId"],
            po_number=po["poNumber"],
            universal_id=po["universalId"],
            invoice_number=req.invoice_number,
            invoice_amount=req.invoice_amount,
            gl_account_id=gl_account_id,
            ap_gl_account_id=ap_gl_account_id,
            ref_text=ref_text,
            po_type="SUBLET",
            sales_tax=req.sales_tax,
        )

        return CreatePoResponse(
            success=True,
            po_number=po["poNumber"],
            po_id=po["poId"],
            po_status=po["status"],
            invoice_id=result["invoiceId"],
            vendor_name=vendor["name"],
        )

    except HTTPException:
        raise
    except Exception as e:
        reset_client()
        return CreatePoResponse(success=False, error=str(e))


def _create_misc_po(
    req: CreateMiscPoRequest,
    session: Session,
) -> CreatePoResponse:
    try:
        client = get_client(session)
        dealer_id = _resolve_dealer(client, req.dealership_name)
        vendor = _resolve_vendor(client, dealer_id, req.vendor_name, session)

        # Build misc items — each line item has its own GL account.
        if req.line_items:
            items = [
                {
                    "description": li.part_name,
                    "qty": li.qty,
                    "price": li.unit_price,
                }
                for li in req.line_items
            ]
        else:
            items = [{"description": "Misc purchase", "qty": 1, "price": req.invoice_amount}]

        po = client.create_misc_po(
            vendor_id=vendor["id"],
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            vendor_site_id=vendor["siteId"],
            vendor_phone=vendor["phone"],
            vendor_email=vendor["email"],
            items=items,
        )

        # Pre-invoice — use the first line item's GL, or default 0021.
        gl_account = req.line_items[0].gl_account if req.line_items else "0021"
        gl_account_id = f"{dealer_id}_{gl_account}"
        ap_gl_account_id = f"{dealer_id}_3002"

        result = client.pre_invoice(
            vendor_id=vendor["id"],
            vendor_site_id=vendor["siteId"],
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            dealer_id=dealer_id,
            po_id=po["poId"],
            po_number=po["poNumber"],
            universal_id=po["universalId"],
            invoice_number=req.invoice_number,
            invoice_amount=req.invoice_amount,
            gl_account_id=gl_account_id,
            ap_gl_account_id=ap_gl_account_id,
            ref_text="",
            po_type="MISCELLANEOUS",
            sales_tax=req.sales_tax,
        )

        return CreatePoResponse(
            success=True,
            po_number=po["poNumber"],
            po_id=po["poId"],
            po_status=po["status"],
            invoice_id=result["invoiceId"],
            vendor_name=vendor["name"],
        )

    except HTTPException:
        raise
    except Exception as e:
        reset_client()
        return CreatePoResponse(success=False, error=str(e))


def _create_stock_po(
    req: CreateStockPoRequest,
    session: Session,
) -> CreatePoResponse:
    try:
        client = get_client(session)
        dealer_id = _resolve_dealer(client, req.dealership_name)
        vendor = _resolve_vendor(client, dealer_id, req.vendor_name, session)

        if not req.parts:
            raise HTTPException(
                status_code=422,
                detail="Stock PO requires at least one part",
            )

        # Fetch dealer address for shipping/billing
        dealer_address = client.get_dealer_address()

        order = client.create_stock_order(
            vendor_id=int(vendor["id"]),
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            vendor_site_id=vendor["siteId"],
            vendor_phone=vendor["phone"],
            vendor_email=vendor["email"],
            parts=[p.model_dump(by_alias=True) for p in req.parts],
            dealer_address=dealer_address,
        )

        return CreatePoResponse(
            success=True,
            po_number=str(order.get("orderNumber") or ""),
            po_id=order.get("orderId"),
            po_status=order.get("status"),
            vendor_name=order.get("vendorName") or vendor["name"],
        )

    except HTTPException:
        raise
    except Exception as e:
        reset_client()
        return CreatePoResponse(success=False, error=str(e))


@router.get("/vendors/{dealer_id}")
def list_vendor_mappings(
    dealer_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> list[dict]:
    """List all vendor mappings for a dealership."""
    rows = session.exec(
        select(VendorMapping).where(VendorMapping.dealer_id == dealer_id)
    ).all()
    return [
        {"vendor_name": r.vendor_name, "vendor_display_id": r.vendor_display_id}
        for r in rows
    ]


@router.post("/vendors")
def add_vendor_mapping(
    dealer_id: str,
    vendor_name: str,
    vendor_display_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Add or update a vendor mapping. Used after human review picks a candidate."""
    normalized = _normalize(vendor_name)
    existing = session.exec(
        select(VendorMapping).where(
            VendorMapping.dealer_id == dealer_id,
            VendorMapping.vendor_name == normalized,
        )
    ).first()

    if existing:
        existing.vendor_display_id = vendor_display_id
        session.add(existing)
    else:
        session.add(
            VendorMapping(
                dealer_id=dealer_id,
                vendor_name=normalized,
                vendor_display_id=vendor_display_id,
            )
        )
    session.commit()
    return {"message": "Vendor mapping saved"}
