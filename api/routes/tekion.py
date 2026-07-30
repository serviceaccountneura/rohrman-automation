"""Tekion routes — sublet and misc PO creation via direct API calls.

POST /api/tekion/po/sublet — create sublet PO + pre-invoice
POST /api/tekion/po/misc   — create misc PO + pre-invoice
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from api.models.schemas import CreateMiscPoRequest, CreatePoResponse, CreateSubletPoRequest
from api.services.tekion_client import TekionApiClient

router = APIRouter(prefix="/api/tekion", tags=["tekion"])

# Shared client — login once, reuse across requests.
# Thread-safe via lock; each request gets exclusive access to the client.
_client: TekionApiClient | None = None
_client_lock = threading.Lock()


def get_client() -> TekionApiClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = TekionApiClient()
            _client.login()
        return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        _client = None


@router.post("/po/sublet", response_model=CreatePoResponse)
def create_sublet_po(req: CreateSubletPoRequest) -> CreatePoResponse:
    try:
        client = get_client()

        # Switch dealer if dealership name provided
        if req.dealership_name:
            dealer_id = client.find_dealer_by_name(req.dealership_name)
            if dealer_id:
                client.switch_dealer(dealer_id)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not match dealership '{req.dealership_name}' to any dealer",
                )

        # Search vendor
        vendors = client.search_vendor(req.vendor_name)
        if not vendors:
            short_name = " ".join(req.vendor_name.split()[:2])
            vendors = client.search_vendor(short_name)
        if not vendors:
            raise HTTPException(status_code=404, detail=f"No vendor found for '{req.vendor_name}'")
        vendor = vendors[0]

        # Search RO
        ros = client.search_ro(req.control_number)
        if not ros:
            raise HTTPException(status_code=404, detail=f"No RO found for '{req.control_number}'")
        ro = ros[0]

        # Get RO jobs
        jobs = client.get_ro_jobs(ro["id"])
        if not jobs:
            raise HTTPException(status_code=404, detail="No jobs found on RO")

        # Pick job by job_type if specified, else first
        job = jobs[0]
        if req.job_type:
            matched = next((j for j in jobs if j["jobNumber"] == req.job_type), None)
            if matched:
                job = matched

        # Build items
        if req.line_items:
            items = [
                {
                    "description": item.description,
                    "jobId": job["id"],
                    "referenceId": ro["id"],
                    "referenceNumber": ro["roNumber"],
                    "opcode": req.opcode,
                    "category": req.category,
                    "laborAmount": item.labor_amount,
                    "partsAmount": item.parts_amount,
                }
                for item in req.line_items
            ]
        else:
            items = [
                {
                    "description": "Sublet repair",
                    "jobId": job["id"],
                    "referenceId": ro["id"],
                    "referenceNumber": ro["roNumber"],
                    "opcode": req.opcode,
                    "category": req.category,
                    "laborAmount": 0,
                    "partsAmount": req.invoice_amount,
                }
            ]

        # Create PO
        po = client.create_sublet_po(
            vendor_id=int(vendor["id"]),
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            vendor_site_id=vendor["siteId"],
            vendor_phone=vendor["phone"],
            vendor_email=vendor["email"],
            items=items,
        )

        # Pre-invoice
        dealer_id = client.current_dealer_id
        gl_account_id = f"{dealer_id}_{req.gl_account}"
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
            ref_text=ro["roNumber"],
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


@router.post("/po/misc", response_model=CreatePoResponse)
def create_misc_po(req: CreateMiscPoRequest) -> CreatePoResponse:
    try:
        client = get_client()

        # Switch dealer if dealership name provided
        if req.dealership_name:
            dealer_id = client.find_dealer_by_name(req.dealership_name)
            if dealer_id:
                client.switch_dealer(dealer_id)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not match dealership '{req.dealership_name}' to any dealer",
                )

        # Search vendor
        vendors = client.search_vendor(req.vendor_name)
        if not vendors:
            short_name = " ".join(req.vendor_name.split()[:2])
            vendors = client.search_vendor(short_name)
        if not vendors:
            raise HTTPException(status_code=404, detail=f"No vendor found for '{req.vendor_name}'")
        vendor = vendors[0]

        # Build items
        if req.line_items:
            items = [
                {
                    "description": item.description,
                    "qty": item.qty,
                    "price": item.unit_price or item.total_price,
                }
                for item in req.line_items
            ]
        else:
            items = [{"description": "Misc purchase", "qty": 1, "price": req.invoice_amount}]

        # Create PO
        po = client.create_misc_po(
            vendor_id=vendor["id"],
            vendor_name=vendor["name"],
            vendor_display_id=vendor["displayId"],
            vendor_site_id=vendor["siteId"],
            vendor_phone=vendor["phone"],
            vendor_email=vendor["email"],
            items=items,
        )

        # Pre-invoice
        dealer_id = client.current_dealer_id
        gl_account_id = f"{dealer_id}_{req.gl_account}"
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
