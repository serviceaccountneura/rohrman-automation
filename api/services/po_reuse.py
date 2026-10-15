"""Match a purchase order the invoice already names (Sublet / Misc).

THE PROBLEM (historical)
    Sublet and Misc invoices used to create a NEW purchase order even when the
    invoice printed the number of one that already existed in Tekion. The clerk
    who raised that PO then had two: the real one, still open, and ours.

THE SHAPE OF THE ANSWER (current)
    Parts raise the PO in Tekion first. We only read the number off the invoice,
    look it up, and pre-invoice against it -- same idea as the vendor stock
    flow. We do not create Sublet/Misc POs, and we do not ask "create a new
    one?". If the number is missing or not in Tekion, the document fails.

        ocr_helpers.get_po_number()          the number on the page
        TekionApiClient.find_purchase_order() the exact-match lookup
        TekionApiClient.pre_invoice()         takes any PO's id/number/universalId

WHAT THE PO RECORD TELLS US
    The search hit carries `invoiceStatus` and `invoiceNumbers`. The second is
    the useful one and is easy to miss:

        "invoiceNumbers": [
            {"invoiceId": "6aa2b5c8...", "invoiceNumber": "989872",
             "paymentTime": 1789048264640}
        ]

    That is what makes "already pre-invoiced" answerable as "already
    pre-invoiced WITH THIS INVOICE", which is the question that matters. A PO
    carrying a different invoice number is a second bill against one order --
    common and legitimate -- while the same number twice is the one thing that
    must not post.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# A PO in one of these states cannot take an invoice at all. Same list the
# stock flow uses -- the lookup deliberately searches every status so this can
# be said plainly rather than reported as "no such PO".
_DEAD_STATUSES = {"CANCELLED", "CANCELED", "VOIDED", "CLOSED"}

# The choice a person made, honoured for exactly one run.
CHOICE_EXISTING = "EXISTING"
CHOICE_NEW = "NEW"

# Why a PO that exists still cannot take this invoice. Two codes rather than
# one, because the headline a person reads should name the actual cause: "this
# PO already has this invoice" and "this PO is cancelled" call for different
# next steps, and a single code forced the UI to describe both at once.
BLOCK_ALREADY_INVOICED = "PO_ALREADY_INVOICED"
BLOCK_CLOSED = "PO_CLOSED"


@dataclass
class FoundPo:
    """A purchase order the invoice named, as Tekion holds it."""

    po_id: Any = None
    po_number: str = ""
    universal_id: str = ""
    order_type: str = ""
    status: str = ""
    invoice_status: str = ""
    vendor_name: str = ""
    total_amount: float | None = None
    # [{"invoiceNumber": "989872", ...}] -- every invoice already on this PO.
    invoice_numbers: list[str] = field(default_factory=list)
    # The whole hit, for the pre-invoice call, which needs vendorDetails.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_dead(self) -> bool:
        return str(self.status or "").upper() in _DEAD_STATUSES

    def carries(self, invoice_number: str) -> bool:
        """Whether this exact invoice is already on the PO.

        Compared case-insensitively and without surrounding space: Tekion holds
        what was typed, and "pf2986" and "PF2986" are one invoice.
        """
        wanted = str(invoice_number or "").strip().lower()
        if not wanted:
            return False
        return any(str(n).strip().lower() == wanted for n in self.invoice_numbers)

    def matches_vendor(self, invoice_vendor: str) -> bool:
        """Whether this PO is with the vendor who sent the invoice.

        Weak evidence deliberately used weakly. Vendor names are not written the
        same way twice -- "S&S AUTOMOTIVE, INC." against "S&S Automotive" --
        so this compares only the alphanumerics, and a difference is SHOWN to
        the person rather than acted on.

        It matters because the number on the page is not always a Tekion PO
        number. Vendors print their own order references under headings like
        "PURCHASE ORDER NUMBER" -- one invoice here carries 071526, the date the
        buyer rang it in -- and a reference like that can collide with a real PO
        at a store whose numbers are the same length. The vendor is what tells
        a person the two are unrelated at a glance.
        """
        mine = re.sub(r"[^a-z0-9]", "", str(invoice_vendor or "").lower())
        theirs = re.sub(r"[^a-z0-9]", "", str(self.vendor_name or "").lower())
        if not mine or not theirs:
            return True  # nothing to disagree about
        return mine.startswith(theirs) or theirs.startswith(mine)

    def as_json(self, invoice_vendor: str = "") -> str:
        """For the document row, so the queue can show this without asking again."""
        return json.dumps(
            {
                "vendorMatches": self.matches_vendor(invoice_vendor),
                "poId": self.po_id,
                "poNumber": self.po_number,
                "universalId": self.universal_id,
                "orderType": self.order_type,
                "status": self.status,
                "invoiceStatus": self.invoice_status,
                "vendorName": self.vendor_name,
                "totalAmount": self.total_amount,
                "invoiceNumbers": self.invoice_numbers,
            }
        )[:2000]


def _invoice_numbers(hit: dict[str, Any]) -> list[str]:
    """The invoice numbers already on a PO.

    Tolerant of shape on purpose. Every example seen is a list of objects with
    an `invoiceNumber` key, but this is an undocumented API and a bare list of
    strings would otherwise read as no invoices at all -- which is the answer
    that lets a double post through.
    """
    numbers: list[str] = []
    for entry in hit.get("invoiceNumbers") or []:
        if isinstance(entry, dict):
            value = entry.get("invoiceNumber") or entry.get("number") or ""
        else:
            value = entry
        text = str(value or "").strip()
        if text and text not in numbers:
            numbers.append(text)
    return numbers


def look_up(client: Any, po_number: str) -> FoundPo | None:
    """The purchase order with this number at the client's current dealership.

    Returns None when there is no such PO. For Sublet/Misc that is a failure
    upstream (we do not create POs); the caller turns it into PO_NOT_FOUND.

    The caller must already have switched dealership. Every field this reads is
    per-store, and a PO number is only unique within one.
    """
    if not str(po_number or "").strip():
        return None

    hit = client.find_purchase_order(po_number)
    if not hit:
        return None

    total = hit.get("totalAmount")
    return FoundPo(
        po_id=hit.get("id"),
        po_number=str(hit.get("orderNumber") or ""),
        universal_id=str(hit.get("universalId") or ""),
        order_type=str(hit.get("orderType") or ""),
        status=str(hit.get("status") or ""),
        invoice_status=str(hit.get("invoiceStatus") or ""),
        vendor_name=str((hit.get("vendorDetails") or {}).get("vendorName") or ""),
        total_amount=float(total) if isinstance(total, (int, float)) else None,
        invoice_numbers=_invoice_numbers(hit),
        raw=hit,
    )


def blocking(found: FoundPo, invoice_number: str) -> tuple[str, str]:
    """Why this PO cannot take this invoice: (code, message), or ("", "") if it can.

    Two things stop it, and they are different in kind:

    A dead PO is a state problem -- cancelled, voided or closed. Tekion will
    refuse it, and saying so here means the message names the actual reason
    rather than relaying whatever the API returns.

    The same invoice number already on the PO is the one that matters. The
    duplicate check upstream catches a repeat of a document WE processed; this
    catches a PO that was invoiced in Tekion directly, by a person, which
    nothing in our own records would show.

    Short, but it names Tekion. "PO 33147 already has invoice 0016978" left the
    reader to work out WHERE that PO is -- on the invoice in front of them, or
    in the system. It is in Tekion, and that is the whole point of the check.
    """
    if found.is_dead:
        return (
            BLOCK_CLOSED,
            f"PO {found.po_number} exists in Tekion but is {found.status.lower()}",
        )
    if found.carries(invoice_number):
        return (
            BLOCK_ALREADY_INVOICED,
            f"PO {found.po_number} already exists in Tekion with invoice "
            f"{invoice_number} on it",
        )
    return "", ""


def describe(found: FoundPo) -> str:
    """One line for the queue, so the choice can be made without opening Tekion."""
    bits = [f"PO {found.po_number}"]
    if found.vendor_name:
        bits.append(found.vendor_name)
    if found.total_amount is not None:
        bits.append(f"${found.total_amount:,.2f}")
    if found.status:
        bits.append(found.status.title())
    if found.invoice_numbers:
        bits.append("invoice " + ", ".join(found.invoice_numbers))
    return " · ".join(bits)


def as_pre_invoice_po(found: FoundPo) -> dict[str, Any]:
    """Search hit shaped for ``client.pre_invoice`` / CreatePoResponse fields.

    Tekion's purchase search returns ``id`` / ``orderNumber``; the create-PO
    response (and pre-invoice call) use ``poId`` / ``poNumber``. Both keys are
    set so either naming works.
    """
    po = dict(found.raw)
    po_id = found.po_id if found.po_id is not None else po.get("id")
    po_number = found.po_number or str(po.get("orderNumber") or "")
    universal = found.universal_id or str(po.get("universalId") or "")
    po["id"] = po_id
    po["poId"] = po_id
    po["orderNumber"] = po_number
    po["poNumber"] = po_number
    po["universalId"] = universal
    po["status"] = found.status or str(po.get("status") or "")
    return po
