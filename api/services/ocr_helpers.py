"""OCR JSON field extraction helpers.

Python port of src/api/ocrHelpers.ts — reads the structure produced by
vision_extract.py and extracts vendor, dealership, invoice number, totals,
line items, and sales tax.
"""
from __future__ import annotations

import json
import re
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
