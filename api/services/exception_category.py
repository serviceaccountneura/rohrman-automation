"""Exception categories for VI-verify — who fixes what.

    PO         — Parts: GL missing, amount/invoice mismatches, closed PO, etc.
    JE         — Accounts Payable: GL 7xxx needs control + description.
    DUPLICATE  — Same invoice more than once (re-upload or near-dup in Tekion).
    WAITING    — PO or pre-invoice not in Tekion yet (auto-retry / wait on Tekion).
    OTHER      — Everything else (unclear OCR, worker abandoned, …).

Also holds invoice-number near-duplicate helpers used by the queue and VI verify.
"""
from __future__ import annotations

import re

CATEGORY_PO = "PO"
CATEGORY_JE = "JE"
CATEGORY_DUPLICATE = "DUPLICATE"
CATEGORY_WAITING = "WAITING"
CATEGORY_OTHER = "OTHER"

CATEGORY_LABELS = {
    CATEGORY_PO: "PO / Invoice",
    CATEGORY_JE: "Journal entry",
    CATEGORY_DUPLICATE: "Duplicates",
    CATEGORY_WAITING: "Waiting on Tekion",
    CATEGORY_OTHER: "Other",
}

# Waiting on Tekion: PO / pre-invoice not there yet — auto-retry in the UI.
_WAITING_CODES = frozenset(
    {
        "PRE_INVOICE_MISSING",
        "PO_NOT_FOUND",
    }
)

# Codes that belong on the Parts / PO side of the dashboard.
_PO_CODES = frozenset(
    {
        "NO_GL_ACCOUNT_WRITTEN",
        "INVOICE_NUMBER_MISMATCH",
        "AMOUNT_MISMATCH",
        "PO_CLOSED",
        "PO_ALREADY_INVOICED",
        "MISSING_FIELD",
        "VENDOR_NOT_FOUND",
        "TEKION_REJECTED",
    }
)

_JE_CODES = frozenset(
    {
        "JE_CONTROL_REQUIRED",
    }
)

_DUPLICATE_CODES = frozenset(
    {
        "DUPLICATE_DOCUMENT",
        "DUPLICATE_INVOICE",
    }
)


def category_for(exception_type: str | None, *, status: str | None = None) -> str:
    """Map a document's exception_type (and status) onto a dashboard bucket."""
    if (status or "").upper() == "DUPLICATE":
        return CATEGORY_DUPLICATE
    code = (exception_type or "").strip().upper()
    if not code:
        return CATEGORY_OTHER
    if code in _DUPLICATE_CODES:
        return CATEGORY_DUPLICATE
    if code in _WAITING_CODES:
        return CATEGORY_WAITING
    if code in _JE_CODES:
        return CATEGORY_JE
    if code in _PO_CODES:
        return CATEGORY_PO
    return CATEGORY_OTHER


def invoice_digits(value: str | None) -> str:
    """Digit core of an invoice number (BLK123 → 123, 6101029RI → 6101029)."""
    return "".join(c for c in str(value or "") if c.isdigit())


def normalize_invoice(value: str | None) -> str:
    """Uppercase, strip spaces — full-string compare still uses this."""
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def invoices_are_near_duplicates(a: str | None, b: str | None) -> bool:
    """True when numbers are the same invoice with optional letter wrap.

    Exact match counts. So does a shared digit core with different letter
    prefixes/suffixes (Auto Glass BLK123 vs 123, SNS …RI vs bare digits).
    Different digit cores never match.
    """
    na, nb = normalize_invoice(a), normalize_invoice(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    da, db = invoice_digits(na), invoice_digits(nb)
    if not da or not db or da != db:
        return False
    # Same digits, different letter wrapping → near-duplicate.
    return True
