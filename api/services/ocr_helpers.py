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


def _clean_ro_number(raw: Any) -> str:
    """Strip the label off a repair order number.

    Invoices print the RO as "RO 1575659", "RO# 1575659", "R.O. 1575659" — and
    OCR routinely reads that leading "RO" as "R0" with a digit zero. Tekion
    searches on the number itself, so a value carrying the prefix finds nothing
    and the run fails with "No RO found" even though the number was read
    correctly.

    Only a recognisable RO label is removed. A number that genuinely contains
    letters is left alone rather than guessed at.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # "RO", "R0", "R.O.", "RO#", "REPAIR ORDER" — optionally followed by
    # punctuation and whitespace, at the start only.
    stripped = re.sub(
        r"^\s*(?:R[O0]|R\.?\s*[O0]\.?|REPAIR\s+ORDER)\s*[#:.\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return stripped or text


def get_vin(ocr: dict[str, Any]) -> str:
    vehicle = ocr.get("vehicle") or {}
    vin = str(vehicle.get("vin") or "").strip().upper()
    # Drop obvious OCR noise ("[illegible]", stray punctuation) rather than
    # feeding a garbage VIN into a Tekion search.
    vin = re.sub(r"[^A-Z0-9]", "", vin)
    return vin if len(vin) >= 11 else ""


# Labels that really do name a repair order, most specific first. Matched as
# whole words against a label stripped of punctuation and spaces.
#
# The obvious `"ro" in label` is what this replaces, and it was wrong in a way
# that is easy to miss: "ro" appears inside "shipped f-r-o-m", so every invoice
# with a Shipped From field filed its shipping origin as a repair order number.
_RO_LABELS = (
    "repairorder",
    "repairordernumber",
    "ronumber",
    "rono",
    "controlnumber",
    "controlno",
)

# Fields that contain an RO label as a substring but mean something else.
_NOT_AN_RO = ("shippedfrom", "shipto", "billto", "salesorder", "purchaseorder")


def _looks_like_ro_label(label: str) -> bool:
    """True when this label names a repair order rather than merely containing 'ro'."""
    key = "".join(str(label or "").lower().split()).replace(".", "").replace("-", "")
    if not key or any(key.startswith(bad) for bad in _NOT_AN_RO):
        return False
    if key in _RO_LABELS:
        return True
    # A bare "RO" or "R/O" column heading is common and legitimate.
    if key in ("ro", "r/o", "ro#"):
        return True
    # Otherwise require the words, not the letters: "repair order" anywhere, or
    # a label that starts with "ro" followed by a separator ("ro no", "ro #").
    return "repairorder" in key or key.startswith("rono") or key.startswith("ro#")


def get_control_number(ocr: dict[str, Any]) -> str:
    po_contract = ocr.get("_po_contract") or {}
    if po_contract.get("ro_number"):
        return _clean_ro_number(po_contract["ro_number"])

    for id_entry in ocr.get("identifiers", []):
        if _looks_like_ro_label(id_entry.get("label")):
            return _clean_ro_number(id_entry.get("value"))

    return _clean_ro_number(
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


def _clean_po_number(raw: Any) -> str:
    """Strip a leading PO label, the way _clean_ro_number does for repair orders."""
    text = str(raw or "").strip()
    if not text:
        return ""
    stripped = re.sub(
        r"^\s*(?:P\.?\s*O\.?|PURCHASE\s+ORDER)\s*(?:NUMBER|NO|#)?\s*[#:.\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return stripped or text


def get_po_number(ocr: dict[str, Any]) -> str:
    """The purchase order number printed on the invoice.

    Vendor stock orders are invoiced against a PO that already exists in Tekion,
    so this is the field the whole flow hangs on.

    "PO BOX" is excluded outright — it is part of the vendor's address and
    appears on most invoices, so a loose match would confidently return a
    postal box number as a purchase order.
    """
    po_contract = ocr.get("_po_contract") or {}
    if po_contract.get("po_number"):
        return _clean_po_number(po_contract["po_number"])

    identifiers = ocr.get("identifiers") or []

    def scan(predicate) -> str:
        for entry in identifiers:
            label = (entry.get("label") or "").strip().lower()
            if not label or "box" in label:
                continue
            if predicate(label):
                cleaned = _clean_po_number(entry.get("value"))
                if cleaned:
                    return cleaned
        return ""

    # An explicit purchase-order label first.
    found = scan(lambda label: "purchase order" in label)
    if found:
        return found
    # Then the abbreviation, but only where it stands as its own word.
    found = scan(
        lambda label: label.replace(".", "").replace(" ", "").startswith("po")
    )
    if found:
        return found

    return _clean_po_number(ocr.get("po_number") or ocr.get("poNumber") or "")



# A dealership GL account is four or five digits. Anything shorter is a line
# number or a quantity; anything longer is an invoice or part number.
_GL_PATTERN = re.compile(r"\b(\d{4,5})\b")

# Labels and note text that introduce a GL account, so "GL# 2410" scrawled in
# the margin is read as an account and "Invoice Number 135624720" is not.
_GL_HINTS = ("gl", "g/l", "account", "acct", "acc#", "post to", "charge to")


def _first_gl(text: Any) -> str:
    """The first plausible GL account in a string, or ""."""
    match = _GL_PATTERN.search(str(text or ""))
    return match.group(1) if match else ""


def get_line_item_gl_accounts(ocr: dict[str, Any]) -> dict[int, str]:
    """GL account per line item index, for the lines that carry one."""
    out: dict[int, str] = {}
    for i, item in enumerate(ocr.get("line_items") or []):
        account = _first_gl(item.get("gl_account"))
        if account:
            out[i] = account
    return out


def get_document_gl_account(ocr: dict[str, Any]) -> str:
    """The GL account written on the invoice, for the whole document.

    Clerks write the account on the page by hand -- "GL# 2410" circled in the
    margin -- and the OCR prompt already binds a code like that to everything on
    the document when no arrow points it at one line. This reads that back.

    Searched most reliable first:
      1. an account every line item agrees on
      2. gl_mappings[], which the prompt fills for fees and charges
      3. an identifier labelled like a GL account
      4. handwritten notes, where a margin scrawl ends up

    Returns "" when the invoice does not name one. Callers treat that as
    "nothing was written here", not as an error.
    """
    per_line = set(get_line_item_gl_accounts(ocr).values())
    if len(per_line) == 1:
        return per_line.pop()

    for mapping in ocr.get("gl_mappings") or []:
        account = _first_gl(mapping.get("gl_account"))
        if account:
            return account

    for entry in ocr.get("identifiers") or []:
        label = str(entry.get("label") or "").lower()
        if any(hint in label for hint in _GL_HINTS):
            account = _first_gl(entry.get("value"))
            if account:
                return account

    # A handwritten note only counts when it says it is an account. Invoices are
    # covered in stray numbers, and picking one at random would post real money
    # to whatever four digits happened to be legible.
    for note in ocr.get("handwritten_notes") or []:
        text = str(note or "").lower()
        if any(hint in text for hint in _GL_HINTS):
            account = _first_gl(note)
            if account:
                return account

    return ""

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


# Labels that look like a total but are not the amount owed. "SUBTOTAL"
# contains "total", so a naive substring match returns the pre-tax figure — and
# anything that then subtracts tax from it takes the tax off twice.
_NOT_A_GRAND_TOTAL = (
    "sub", "line", "item", "extended", "tax", "discount", "rate",
    "qty", "quantity", "freight", "shipping", "handling", "paid", "credit",
)

# Tried in order. An explicit grand-total label beats a bare "total".
_GRAND_TOTAL_LABELS = (
    "grand total", "total amount due", "amount due", "balance due",
    "invoice total", "total due", "total amount", "net due", "please pay",
)


def get_total_amount(ocr: dict[str, Any]) -> float:
    """The amount owed on the invoice, tax included.

    Label matching is deliberately prioritised rather than first-match: an
    invoice prints several numbers that read as totals, and picking the wrong
    one is not a rounding error. One S&S invoice listed EXTENDED 411.26, SALES
    TAX 34.96 and TOTAL 446.22; returning 411.26 as "the total" and then
    deducting tax produced a purchase order for 376.30 against a 446.22 bill.
    """
    totals = ocr.get("totals") or []

    def value_for(predicate) -> float | None:
        for entry in totals:
            label = (entry.get("label") or "").strip().lower()
            if not label or any(bad in label for bad in _NOT_A_GRAND_TOTAL):
                continue
            if predicate(label):
                amount = _parse_amount(entry.get("value"))
                if amount:
                    return amount
        return None

    # 1. An unambiguous grand-total label.
    for wanted in _GRAND_TOTAL_LABELS:
        found = value_for(lambda label, w=wanted: w in label)
        if found is not None:
            return found

    # 2. The word "total" on its own.
    found = value_for(lambda label: label == "total")
    if found is not None:
        return found

    # 3. Any surviving label containing "total" — subtotals and line totals
    #    were excluded above.
    found = value_for(lambda label: "total" in label)
    if found is not None:
        return found

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
                "glAccount": item.get("gl_account") or "",
            }
        )
    return result
