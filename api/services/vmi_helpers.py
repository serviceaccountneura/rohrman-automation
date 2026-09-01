"""Read a vehicle manufacturer invoice's OCR into `VehicleInvoiceFacts`.

Separate from `ocr_helpers.py` because the questions are different. A parts
invoice asks "what is the total and which GL does it belong to". A vehicle
invoice asks "what is the stock number, what did the car cost the dealer, and
which of the amounts the clerk wrote in the margin is the holdback".

WHY THE SEARCH IS SO TOLERANT
    The OCR contract is not a fixed schema -- Gemini is asked to mirror the
    document's own structure, so a Kia invoice nests things differently from a
    Ford one, and the same manufacturer changes layout between months. Rather
    than chase that, every lookup here walks the whole tree looking for a
    label/value pair whose label matches, at any depth.

    The cost of tolerance is false positives, so each extractor is narrow about
    what it will accept: amounts must sit next to a label that names them,
    and the stock number must look like a stock number.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from api.services import ocr_helpers
from api.services.vmi_je_creation import VehicleInvoiceFacts, detect_manufacturer

# ── Walking the OCR tree ─────────────────────────────────────────────────────


def _pairs(node: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Every (label, value) in the OCR document, at any depth.

    Two shapes count as a pair: a dict key with a scalar value, and the
    {"label": ..., "value": ...} rows the prompt produces for totals and
    identifiers. Both appear in real output, often in the same document.
    """
    if isinstance(node, dict):
        label = node.get("label") or node.get("description") or node.get("name")
        if label is not None and "value" in node:
            yield str(label), node["value"]
        if label is not None and "amount" in node:
            yield str(label), node["amount"]
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _pairs(v, str(k))
            else:
                yield str(k), v
    elif isinstance(node, list):
        for item in node:
            yield from _pairs(item, key)


def _normalise(text: Any) -> str:
    """Lowercase, punctuation-free, space-free — for substring matching."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _amount(value: Any) -> float | None:
    """A dollar amount, or None. Unlike `ocr_helpers._parse_amount`, this keeps
    the sign and returns None rather than 0.0 for junk — a missing holdback and
    a zero holdback lead to different decisions."""
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return round(-abs(amount) if negative else amount, 2)


def _find_amount(ocr: dict[str, Any], hints: tuple[str, ...]) -> float | None:
    """The first amount whose label contains one of `hints`."""
    for label, value in _pairs(ocr):
        flat = _normalise(label)
        if any(hint in flat for hint in hints):
            amount = _amount(value)
            if amount is not None:
                return abs(amount)
    return None


# ── The individual facts ─────────────────────────────────────────────────────

# "DEALER COST" is the column the entry is built from. MSRP is the customer
# price and posting it would overstate inventory by the markup.
_DEALER_COST_HINTS = ("dealercost", "dealerinvoice", "dealertotal", "totaldealercost")
_MSRP_HINTS = ("msrp", "retailtotal", "totalmsrp")

# Amounts a clerk writes on the invoice. The keys match what a template asks
# for in `ManufacturerTemplate.requires`.
_ANNOTATED_AMOUNT_HINTS: dict[str, tuple[str, ...]] = {
    "holdback": ("holdback", "holdbk", "hb"),
    "krs": ("krs", "retailsupport", "kiaretailsupport"),
    "doc_fee": ("docfee", "documentfee", "internaldoc", "docfeepayable"),
    "htv": ("htv", "holdbacktovehicle"),
    "marketing": ("marketing", "marketingallowance", "advertising", "adassessment"),
    "floorplan": ("floorplan", "floorplancredit", "flooring"),
}

# A stock number as the stores write it: two or three letters then digits
# ("SK6459"), or a bare digit run. Anchored so it does not match a fragment of
# the VIN, which is 17 characters of exactly this alphabet.
_STOCK_PATTERN = re.compile(r"\b([A-Z]{1,3}\d{3,6})\b")
_STOCK_LABEL_HINTS = ("stocknumber", "stockno", "stock", "stk")


# Labels that carry a dealer cost but not the FINAL one. A Kia memorandum
# invoice prints SUBTOTAL 30,638.00 above TOTAL 32,133.00, the difference being
# inland freight -- and taking the first match found the subtotal, which would
# have understated inventory by the freight on every car.
_NOT_A_FINAL_COST = ("sub", "base", "unit", "option", "freight", "handling")


def get_dealer_cost_total(ocr: dict[str, Any]) -> float:
    """The FINAL dealer cost -- what the dealership owes for the car.

    Every candidate is collected rather than taking the first, because these
    invoices print the dealer-cost column several times down the page (base,
    subtotal, total) and first-match lands on whichever the OCR happened to
    emit first. Subtotal-ish labels are dropped, and the largest of what
    remains wins: the total always exceeds its own subtotal.
    """
    candidates: list[float] = []
    for label, value in _pairs(ocr):
        flat = _normalise(label)
        if not any(hint in flat for hint in _DEALER_COST_HINTS):
            continue
        if any(bad in flat for bad in _NOT_A_FINAL_COST):
            continue
        amount = _amount(value)
        if amount is not None and amount > 0:
            candidates.append(abs(amount))
    if candidates:
        return max(candidates)

    # Fall back to the document total. On a memorandum invoice the printed
    # TOTAL is the dealer cost -- the MSRP sits in its own labelled column.
    return ocr_helpers.get_total_amount(ocr)


def get_msrp_total(ocr: dict[str, Any]) -> float:
    return _find_amount(ocr, _MSRP_HINTS) or 0.0


def get_stock_number(ocr: dict[str, Any]) -> str:
    """The stock number, which is nearly always handwritten.

    Labelled fields first, then handwriting. Bare text is searched last and only
    for the letters-then-digits shape, because an unlabelled number on a vehicle
    invoice is far more likely to be an order or key number.
    """
    for label, value in _pairs(ocr):
        if any(hint in _normalise(label) for hint in _STOCK_LABEL_HINTS):
            match = _STOCK_PATTERN.search(str(value or "").upper())
            if match:
                return match.group(1)
            text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
            if text and len(text) <= 10:
                return text

    for note in ocr.get("handwritten_notes") or []:
        match = _STOCK_PATTERN.search(str(note or "").upper())
        if match:
            return match.group(1)

    return ""


def get_annotated_amounts(ocr: dict[str, Any]) -> dict[str, float]:
    """The amounts a clerk wrote on the invoice, keyed by what they are.

    Only labelled values are taken. The handwriting on these invoices is a mix
    of GL account numbers, a stock number and dollar amounts, and guessing which
    bare number is the holdback would post an invented figure to a real
    receivable account.
    """
    found: dict[str, float] = {}
    for key, hints in _ANNOTATED_AMOUNT_HINTS.items():
        amount = _find_amount(ocr, hints)
        if amount is not None:
            found[key] = amount
    return found


def get_gl_annotations(ocr: dict[str, Any]) -> dict[str, float]:
    """Handwritten GL account -> the amount the clerk pointed it at.

    This is the heart of the vehicle flow. Staff write an account number on the
    invoice and draw an arrow to the figure that belongs in it: "2245" against
    KAC0780KAC means 780.00 goes to holdback receivable. The OCR prompt asks for
    these as a `gl_annotations` array; this reads it back.

    Amounts come back positive. Whether a line is a debit or a credit is the
    template's business, not the annotation's.
    """
    found: dict[str, float] = {}
    # gl_mappings[] is the field the vision schema actually defines, and the
    # prompt's arrow-anchoring rules already aim at it. gl_annotations is
    # accepted as an alias so a future schema change does not silently return
    # nothing here.
    entries = list(ocr.get("gl_mappings") or []) + list(ocr.get("gl_annotations") or [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account = re.sub(r"[^0-9A-Za-z]", "", str(entry.get("gl_account") or ""))
        amount = _amount(entry.get("amount"))
        # A 4-5 digit account is the shape every Rohrman GL uses; anything else
        # is a stock number or a dealer code that drifted into the array.
        if not re.fullmatch(r"\d{4,5}[A-Za-z]?", account) or amount is None:
            continue
        found[account] = abs(amount)
    return found


def get_annotated_gl_accounts(ocr: dict[str, Any]) -> list[str]:
    """GL account numbers written on the invoice, in reading order.

    Advisory for now: the template decides which accounts the entry uses. These
    are surfaced so a mismatch between what the clerk wrote and what the
    template chose is visible on the document detail page.
    """
    accounts: list[str] = []
    for note in ocr.get("handwritten_notes") or []:
        for match in re.finditer(r"\b(\d{4,5})\b", str(note or "")):
            account = match.group(1)
            if account not in accounts:
                accounts.append(account)
    document_level = ocr_helpers.get_document_gl_account(ocr)
    if document_level and document_level not in accounts:
        accounts.insert(0, document_level)
    return accounts


# ── Assembling the whole thing ───────────────────────────────────────────────


def build_facts(ocr: dict[str, Any], dealership_name: str = "") -> VehicleInvoiceFacts:
    """Everything the templates are allowed to draw on, from one OCR result."""
    vendor_name = ocr_helpers.get_vendor_name(ocr)
    dealership = dealership_name or ocr_helpers.get_dealership_name(ocr)

    return VehicleInvoiceFacts(
        invoice_number=ocr_helpers.get_invoice_number(ocr),
        invoice_date=ocr_helpers.get_invoice_date(ocr),
        dealership_name=dealership,
        manufacturer=detect_manufacturer(vendor_name, dealership),
        vin=ocr_helpers.get_vin(ocr),
        stock_number=get_stock_number(ocr),
        dealer_cost_total=get_dealer_cost_total(ocr),
        msrp_total=get_msrp_total(ocr),
        annotated_amounts=get_annotated_amounts(ocr),
        annotated_gl_accounts=get_annotated_gl_accounts(ocr),
        gl_annotations=get_gl_annotations(ocr),
    )
