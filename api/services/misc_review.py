"""Review a Misc invoice before anything is posted to Tekion.

WHAT CHANGES
    A Misc invoice used to go from OCR straight to a purchase order and a
    pre-invoice. Whatever the reading got wrong -- an amount, an account, a
    vendor -- was posted, and correcting it meant cancelling in Tekion by hand.

    Now the flow stops once it has decided what it WOULD post. The document
    waits in AWAITING_REVIEW with a draft: the fields it read and the GL lines
    it chose. A person can correct either, and nothing reaches Tekion until they
    say so.

THE DRAFT
    {
      "dealerId": "1707",
      "fields": {"vendorName", "invoiceNumber", "invoiceDate",
                 "invoiceAmount", "salesTax", "dealershipName"},
      "lines":  [{"glAccount", "glName", "amount", "description", "control"}],
      "source": how the lines were chosen,
      "edited": whether a person has changed anything,
      "error":  why the last attempt to post was refused, if it was
    }

    `lines` are the EXPENSE side only. The A/P credit is not a line a person
    edits: it is always the invoice total against the store's A/P account, and
    Tekion adds it itself.

HOW THE LINES MUST ADD UP
    Tekion accepts an invoice's accounting split in two shapes, and both are
    already in use here:

      * the lines total the WHOLE invoice, tax included -- the clerk put the
        tax in its own account. "GL 7193 $2,378.11 / GL 3142 $202.12" on a
        $2,580.23 invoice is this.
      * the lines total the invoice LESS tax, and Tekion posts the tax itself.
        Every Misc invoice without a written split has always posted this way.

    So a draft balances when its lines match either figure, and the review
    screen says which. Anything else is refused before it is sent, rather than
    discovered after a purchase order has been created.
"""
from __future__ import annotations

import json
from typing import Any

# Rounding a sum of cents can leave a stray fraction; anything under half a cent
# is the same amount.
_TOLERANCE = 0.005

MODE_GROSS = "tax_in_lines"
MODE_NET = "tax_by_tekion"
MODE_OFF = "unbalanced"

_FIELD_KEYS = (
    "vendorName",
    "invoiceNumber",
    "invoiceDate",
    "invoiceAmount",
    "salesTax",
    "dealershipName",
)


def _money(value: Any) -> float:
    """A number from whatever a form sent: 2378.11, "2,378.11", "$2,378.11"."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        return round(float(text), 2)
    except ValueError:
        return 0.0


def _account(value: Any) -> str:
    """An account number as typed: "GL# 7193" and " 7193 " are both 7193."""
    text = str(value or "").strip().upper()
    for prefix in ("GL#", "GL #", "GL", "#"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def clean_line(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "glAccount": _account(raw.get("glAccount")),
        "glName": str(raw.get("glName") or "").strip(),
        "amount": _money(raw.get("amount")),
        "description": str(raw.get("description") or "").strip()[:200],
        "control": str(raw.get("control") or "").strip()[:50],
    }


def new_draft(
    *,
    dealer_id: str,
    fields: dict[str, Any],
    lines: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    return {
        "dealerId": dealer_id,
        "fields": {
            "vendorName": str(fields.get("vendorName") or ""),
            "invoiceNumber": str(fields.get("invoiceNumber") or ""),
            "invoiceDate": str(fields.get("invoiceDate") or ""),
            "invoiceAmount": _money(fields.get("invoiceAmount")),
            "salesTax": _money(fields.get("salesTax")),
            "dealershipName": str(fields.get("dealershipName") or ""),
        },
        "lines": [clean_line(line) for line in lines],
        "source": source,
        "edited": False,
        "error": "",
    }


def load(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dump(draft: dict[str, Any]) -> str:
    return json.dumps(draft, default=str)[:8000]


def apply_edit(
    draft: dict[str, Any],
    *,
    fields: dict[str, Any] | None,
    lines: list[dict[str, Any]] | None,
    names: dict[str, str],
) -> dict[str, Any]:
    """Merge a person's edit into the draft.

    Fields MERGE: a form that sends three of five fields changes three. Lines
    REPLACE: the editor always sends the whole table, because removing a line is
    an edit too and a merge could never express it.

    The dealership is deliberately not editable. It decides the entire chart of
    accounts every line is checked against, so changing it would silently
    invalidate the lines already there.

    Nor is the invoice date. The Misc pre-invoice takes its date from Tekion's
    own getInvoiceDate and only falls back to the one read off the page, so a
    corrected date here would be shown and then quietly not used.
    """
    updated = dict(draft)
    if fields is not None:
        merged = dict(draft.get("fields") or {})
        for key in _FIELD_KEYS:
            if key in ("dealershipName", "invoiceDate") or key not in fields:
                continue
            if key in ("invoiceAmount", "salesTax"):
                merged[key] = _money(fields[key])
            else:
                merged[key] = str(fields[key] or "").strip()
        updated["fields"] = merged

    if lines is not None:
        cleaned = [clean_line(line) for line in lines]
        # The name comes from the chart, never from the form -- a person types
        # an account NUMBER, and what it is called is Tekion's to say.
        for line in cleaned:
            line["glName"] = names.get(line["glAccount"], "")
        updated["lines"] = [
            line for line in cleaned if line["glAccount"] or line["amount"]
        ]

    updated["edited"] = True
    updated["error"] = ""
    return updated


def balance(draft: dict[str, Any]) -> dict[str, Any]:
    """How the lines add up against the invoice, and whether that is postable."""
    fields = draft.get("fields") or {}
    total = _money(fields.get("invoiceAmount"))
    tax = _money(fields.get("salesTax"))
    net = round(total - tax, 2)
    lines_total = round(sum(_money(l.get("amount")) for l in draft.get("lines") or []), 2)

    if total and abs(lines_total - total) < _TOLERANCE:
        mode, difference = MODE_GROSS, 0.0
    elif tax and abs(lines_total - net) < _TOLERANCE:
        mode, difference = MODE_NET, 0.0
    else:
        mode, difference = MODE_OFF, round(lines_total - total, 2)

    return {
        "linesTotal": lines_total,
        "invoiceTotal": total,
        "salesTax": tax,
        "net": net,
        "mode": mode,
        "difference": difference,
    }


def problems(draft: dict[str, Any], known_accounts: set[str]) -> list[str]:
    """Everything that would stop this draft posting, in plain words.

    Checked before the document is released to the worker, so a person hears
    about a mistyped account while they are still looking at the screen --
    rather than after a purchase order has been created in Tekion and the
    pre-invoice against it has failed.
    """
    fields = draft.get("fields") or {}
    lines = draft.get("lines") or []
    found: list[str] = []

    if not str(fields.get("vendorName") or "").strip():
        found.append("The vendor is blank.")
    if not str(fields.get("invoiceNumber") or "").strip():
        found.append("The invoice number is blank.")
    if _money(fields.get("invoiceAmount")) <= 0:
        found.append("The invoice total must be more than zero.")
    if not lines:
        found.append("There are no GL lines to post.")

    for index, line in enumerate(lines, start=1):
        account = line.get("glAccount") or ""
        if not account:
            found.append(f"Line {index} has no GL account.")
        elif known_accounts and account not in known_accounts:
            found.append(f"Line {index}: GL {account} is not an account at this dealership.")
        if _money(line.get("amount")) == 0:
            found.append(f"Line {index} has no amount.")

    summary = balance(draft)
    if lines and summary["mode"] == MODE_OFF:
        target = (
            f"${summary['invoiceTotal']:,.2f}"
            + (f" (or ${summary['net']:,.2f} before tax)" if summary["salesTax"] else "")
        )
        found.append(
            f"The lines total ${summary['linesTotal']:,.2f} but must total {target}."
        )

    return found
