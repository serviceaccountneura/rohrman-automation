"""OCR JSON field extraction helpers.

Python port of src/api/ocrHelpers.ts — reads the structure produced by
vision_extract.py and extracts vendor, dealership, invoice number, totals,
line items, and sales tax.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


def load_ocr_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_vendor_name(ocr: dict[str, Any]) -> str:
    vendor = ocr.get("vendor") or {}
    return vendor.get("name") or vendor.get("displayName") or ""


def get_dealership_name(ocr: dict[str, Any]) -> str:
    user_input = ocr.get("user_input") or {}
    dealership = ocr.get("dealership") or {}
    if isinstance(dealership, dict):
        return user_input.get("dealership") or dealership.get("name") or ""
    return user_input.get("dealership") or dealership or ""


def get_control_number(ocr: dict[str, Any]) -> str:
    po_contract = ocr.get("_po_contract") or {}
    if po_contract.get("ro_number"):
        return str(po_contract["ro_number"]).strip()

    for id_entry in ocr.get("identifiers", []):
        label = (id_entry.get("label") or "").lower()
        if "ro" in label or "repair order" in label or "control" in label:
            return str(id_entry.get("value") or "").strip()

    return (
        ocr.get("control_number")
        or ocr.get("controlNumber")
        or (po_contract.get("control_number") or "")
    )


def _clean_invoice_number(raw: str) -> str:
    trimmed = raw.strip()
    first_token = trimmed.split()[0] if trimmed.split() else trimmed
    return first_token or trimmed


def get_invoice_number(ocr: dict[str, Any]) -> str:
    for id_entry in ocr.get("identifiers", []):
        label = (id_entry.get("label") or "").lower()
        if "invoice number" in label or "invoice #" in label or label == "invoice":
            return _clean_invoice_number(str(id_entry.get("value") or ""))

    for id_entry in ocr.get("identifiers", []):
        label = (id_entry.get("label") or "").lower()
        if "invoice" in label and "date" not in label:
            return _clean_invoice_number(str(id_entry.get("value") or ""))

    po_contract = ocr.get("_po_contract") or {}
    fallback = (
        ocr.get("invoice_number")
        or ocr.get("invoiceNumber")
        or po_contract.get("invoice_number")
        or ""
    )
    return _clean_invoice_number(str(fallback)) if fallback else ""


def get_document_type(ocr: dict[str, Any]) -> str:
    """Whatever OCR decided the document is.

    Recorded next to the upload folder so a mismatch stays visible. The folder
    is authoritative for pipeline selection — this is only for review.
    """
    po_contract = ocr.get("_po_contract") or {}
    return str(ocr.get("document_type") or po_contract.get("document_type") or "").strip()


# Date formats seen on vendor invoices and parts tickets, in priority order.
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y")


def _normalize_date(raw: Any) -> str:
    """Normalize a date string to MM/DD/YYYY. Returns '' when unparseable."""
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    # Last resort: pull an M/D/Y out of a longer string ("Invoice Date 05/12/2026").
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if m:
        month, day, year = m.groups()
        if len(year) == 2:
            year = f"20{year}"
        try:
            return datetime(int(year), int(month), int(day)).strftime("%m/%d/%Y")
        except ValueError:
            return ""
    return ""


def get_invoice_date(ocr: dict[str, Any]) -> str:
    """The invoice date as MM/DD/YYYY — the JE flow's Accounting Date.

    Looks for an explicit invoice-date identifier first, then any date label
    that is not a due/payment date, since posting to the due date would put the
    entry in the wrong period.
    """
    identifiers = ocr.get("identifiers") or []

    for entry in identifiers:
        label = (entry.get("label") or "").lower()
        if "invoice date" in label or label in ("date", "inv date"):
            normalized = _normalize_date(entry.get("value"))
            if normalized:
                return normalized

    for entry in identifiers:
        label = (entry.get("label") or "").lower()
        if "date" not in label:
            continue
        if any(skip in label for skip in ("due", "payment", "ship", "order", "delivery")):
            continue
        normalized = _normalize_date(entry.get("value"))
        if normalized:
            return normalized

    po_contract = ocr.get("_po_contract") or {}
    return _normalize_date(
        ocr.get("invoice_date") or ocr.get("invoiceDate") or po_contract.get("invoice_date")
    )


def _parse_amount(value: Any) -> float:
    if isinstance(value, (int, float)):
        return abs(float(value))
    cleaned = re.sub(r"[$,]", "", str(value or "0"))
    try:
        return abs(float(cleaned))
    except ValueError:
        return 0.0


def get_sales_tax(ocr: dict[str, Any]) -> float:
    for t in ocr.get("totals", []):
        label = (t.get("label") or "").lower()
        if "sales tax" in label or label == "tax":
            return _parse_amount(t.get("value"))
    return 0.0


def get_total_amount(ocr: dict[str, Any]) -> float:
    for t in ocr.get("totals", []):
        label = (t.get("label") or "").lower()
        if "grand total" in label or "total" in label or "balance due" in label:
            return _parse_amount(t.get("value"))

    po_contract = ocr.get("_po_contract") or {}
    summary = ocr.get("summary") or {}
    fallback = ocr.get("total") or po_contract.get("total") or summary.get("total") or 0
    return abs(float(fallback))


def to_flat_fields(ocr: dict[str, Any]) -> dict[str, Any]:
    """Flatten the OCR output into the shape the API documents and returns.

    The model picks its own field names, so the raw extraction is nested and
    label-driven: `vendor: {name}`, `identifiers: [{label, value}]`,
    `totals: [{label, value}]`. Every consumer would otherwise have to redo this
    digging — including the frontend, in JavaScript.

    This is the single flattening, used by both the pipeline and the OCR job
    endpoint, so both report identical values.
    """
    return {
        "document_type": get_document_type(ocr),
        "dealership_name": get_dealership_name(ocr),
        "vendor_name": get_vendor_name(ocr),
        "invoice_number": get_invoice_number(ocr),
        "invoice_date": get_invoice_date(ocr),
        "invoice_amount": get_total_amount(ocr),
        "sales_tax": get_sales_tax(ocr),
        "ro_number": get_control_number(ocr),
        "line_items": get_raw_line_items(ocr),
        "needs_review": bool((ocr.get("_validation") or {}).get("needs_review", False)),
    }


def get_raw_line_items(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    items = ocr.get("line_items") or []
    result = []
    for item in items:
        try:
            qty = float(re.sub(r"[$,]", "", str(item.get("qty", "1")))) or 1.0
        except ValueError:
            qty = 1.0
        unit_price = _parse_amount(item.get("unit_price") or item.get("unitPrice"))
        total_price = _parse_amount(item.get("total_price") or item.get("totalPrice"))
        result.append(
            {
                "description": item.get("description") or "",
                "qty": qty,
                "unitPrice": unit_price,
                "totalPrice": total_price,
            }
        )
    return result
