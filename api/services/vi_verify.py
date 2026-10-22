"""Verify an uploaded invoice against an already pre-invoiced Vendor Invoice.

Parts raise the PO and pre-invoice in Tekion first. This module does the last
step only:

  1. Find the Vendor Invoice by **invoice number** on the Pre Invoice list
     (primary). OCR often has no PO #, or misreads a date as a PO #.
  2. Read the linked PO # from that VI; optionally confirm the PO still exists.
  3. Three-way compare (uploaded invoice == Tekion PO == Pre-invoice VI)
     for: PO #, Vendor, Invoice #, Invoice amount (exact cents), Invoice date.
  4. JE screen (postings): Balance must be $0.00; expense GL Control must
     equal the PO vendor name — except GL 2460 (Sublet) = RO #. GLs starting
     with 7 also need a description (control checks behind ENFORCE_JE_CONTROL).
  5. On pass (and writes on): attach the PDF and complete the VI / AP posting.

Never creates a PO. Never calls preInvoice/post.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from api.services.ap_approval import ApApprovalService, ExpectedInvoice
from api.services.exception_category import (
    invoice_digits,
    invoices_are_near_duplicates,
    normalize_invoice,
)
from api.services.tekion_client import TekionApiClient


# Codes returned to the pipeline (map 1:1 onto EX_* where possible).
CODE_OK = ""
CODE_PO_NOT_FOUND = "PO_NOT_FOUND"
CODE_PRE_INVOICE_MISSING = "PRE_INVOICE_MISSING"
CODE_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
CODE_INVOICE_NUMBER_MISMATCH = "INVOICE_NUMBER_MISMATCH"
CODE_PO_NUMBER_MISMATCH = "PO_NUMBER_MISMATCH"
CODE_VENDOR_MISMATCH = "VENDOR_MISMATCH"
CODE_DATE_MISMATCH = "INVOICE_DATE_MISMATCH"
CODE_NO_GL = "NO_GL_ACCOUNT_WRITTEN"
CODE_UNBALANCED = "UNBALANCED_ENTRY"
CODE_JE_CONTROL = "JE_CONTROL_REQUIRED"
CODE_DUPLICATE = "DUPLICATE_INVOICE"
CODE_TEKION = "TEKION_ERROR"

# Same tolerance as OEM JE create — Tekion JE screen shows Balance: 0.0.
_BALANCE_TOLERANCE = 0.005

# Sublet expense GL — Control must be the Repair Order number, not vendor.
SUBLET_GL = "2460"

# TEMP: Parts are not filling Control on Misc pre-invoices yet. Set True to
# restore vendor-name / 2460=RO / 7xxx-description checks.
ENFORCE_JE_CONTROL = False


@dataclass
class ViVerifyInput:
    po_number: str
    invoice_number: str
    invoice_amount: float
    dealership_name: str | None = None
    po_type: str = "MISCELLANEOUS"
    ro_number: str | None = None
    vendor_name: str | None = None
    # OCR / dashboard invoice date (MM/DD/YYYY or ISO).
    invoice_date: str | None = None
    # Used as the posting description when completing the VI.
    reference_number: str = ""
    invoice_file_path: str | None = None
    invoice_file_name: str | None = None


@dataclass
class ViVerifyResult:
    ok: bool = False
    code: str = CODE_OK
    message: str = ""
    invoice_id: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    vendor_name: str | None = None
    tekion_amount: float | None = None
    po_total: float | None = None
    invoice_date: str | None = None
    tekion_invoice_date: str | None = None
    postings: list[dict[str, Any]] = field(default_factory=list)
    media_id: str | None = None
    completed: bool = False
    notes: list[str] = field(default_factory=list)
    # Snapshot for the dashboard three-way match panel.
    match: dict[str, Any] = field(default_factory=dict)

    @property
    def gl_lines_for_doc(self) -> list[dict[str, Any]]:
        """Shape for pipeline_service._record_postings."""
        lines: list[dict[str, Any]] = []
        for p in self.postings:
            gid = str(p.get("glAccountId") or "")
            account = gid.split("_")[-1] if gid else ""
            amount = p.get("amount")
            try:
                amount_f = float(amount) if amount is not None else 0.0
            except (TypeError, ValueError):
                amount_f = 0.0
            lines.append(
                {
                    "glAccount": account,
                    "glName": str(p.get("description") or ""),
                    "amount": round(amount_f, 2),
                    "control": str(p.get("refText") or ""),
                    "source": "Tekion VI postings",
                }
            )
        return lines


def _norm_invoice(value: str | None) -> str:
    """Full invoice number match: trim only. Prefixes/suffixes must stay."""
    return str(value or "").strip()


def _invoice_numbers_equal(a: str | None, b: str | None) -> bool:
    left, right = _norm_invoice(a), _norm_invoice(b)
    if not left or not right:
        return False
    return left == right or left.lower() == right.lower()


def _po_num_from_vi_hit(hit: dict[str, Any]) -> str:
    """PO number Tekion linked on the Vendor Invoice (`poDetails`)."""
    for po in hit.get("poDetails") or []:
        if not isinstance(po, dict):
            continue
        num = str(po.get("poNum") or po.get("orderNumber") or "").strip()
        if num:
            return num
    return str(hit.get("poNumber") or hit.get("orderNumber") or "").strip()


def find_pre_invoice_by_invoice_number(
    svc: ApApprovalService,
    invoice_number: str,
) -> dict[str, Any] | None:
    """Locate a PRE_INVOICED Vendor Invoice by invoice number only.

    Does not match on PO # — OCR PO guesses (e.g. a date misread as PO) must
    not pull the wrong row.
    """
    needle = _norm_invoice(invoice_number)
    if not needle:
        return None

    def _exact(hit: dict[str, Any]) -> bool:
        return _invoice_numbers_equal(hit.get("invoiceNumber"), needle)

    for hit in svc.search_pre_invoiced(search_text=needle):
        if _exact(hit):
            print(f"[VI] Matched Pre Invoice by invoice number {needle!r}")
            return hit

    print("[VI] Invoice search empty; scanning Pre Invoice list…")
    for hit in svc.search_pre_invoiced():
        if _exact(hit):
            print(f"[VI] Matched Pre Invoice on full scan {needle!r}")
            return hit
    return None


def _norm_name(value: str | None) -> str:
    """Compare vendor / control names case-insensitively, collapse spaces."""
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _vendor_core(value: str | None) -> str:
    """Strip Tekion '1707_461 - NAME' prefixes so Control can match PO vendor."""
    raw = str(value or "").strip()
    if " - " in raw:
        raw = raw.split(" - ", 1)[-1].strip()
    return _norm_name(raw)


def _names_match(a: str | None, b: str | None) -> bool:
    ca, cb = _vendor_core(a), _vendor_core(b)
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def _parse_date(value: Any) -> date | None:
    """OCR string or Tekion ms epoch → calendar date."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ms = float(value)
        # Seconds vs milliseconds.
        if ms < 1e12:
            ms *= 1000.0
        try:
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    # ISO with time
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _fmt_date(d: date | None) -> str:
    if not d:
        return ""
    return d.strftime("%m/%d/%Y")


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        raw = value.get("amount")
        if raw is None:
            return None
        try:
            # Tekion often stores cents on VI search hits.
            return float(raw) / 100.0
        except (TypeError, ValueError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _expense_postings(postings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in postings:
        if (p.get("metaData") or {}).get("apPostingLine"):
            continue
        out.append(p)
    return out


def _account_number(posting: dict[str, Any]) -> str:
    gid = str(posting.get("glAccountId") or "")
    return gid.split("_")[-1] if gid else ""


def _posting_amount(posting: dict[str, Any]) -> float:
    raw = posting.get("amount")
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _postings_balance(postings: list[dict[str, Any]]) -> tuple[float, float, float]:
    """Credit / debit / net from Tekion JE lines (negative amount = credit)."""
    credit = 0.0
    debit = 0.0
    for p in postings:
        amt = _posting_amount(p)
        if amt < 0:
            credit += abs(amt)
        else:
            debit += amt
    return round(credit, 2), round(debit, 2), round(debit - credit, 2)


def _money_eq(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return round(float(a), 2) == round(float(b), 2)


def _po_nums_equal(a: str | None, b: str | None) -> bool:
    left, right = str(a or "").strip(), str(b or "").strip()
    if not left or not right:
        return False
    return left == right


def _invoice_numbers_on_po(po: dict[str, Any] | None) -> list[str]:
    """Invoice numbers already listed on the Tekion PO."""
    if not po:
        return []
    from api.services.po_reuse import _invoice_numbers

    return list(_invoice_numbers(po))


def _po_date_for_invoice(po: dict[str, Any] | None, invoice_number: str) -> date | None:
    """Invoice date on the PO's invoiceNumbers entry, when Tekion provides one."""
    if not po:
        return None
    wanted = _norm_invoice(invoice_number).lower()
    for entry in po.get("invoiceNumbers") or []:
        if not isinstance(entry, dict):
            continue
        num = str(entry.get("invoiceNumber") or entry.get("number") or "").strip().lower()
        if wanted and num != wanted:
            continue
        parsed = _parse_date(entry.get("invoiceDate") or entry.get("date"))
        if parsed:
            return parsed
    return _parse_date(po.get("invoiceDate"))


def _build_match_snapshot(
    *,
    invoice_po: str,
    po_po: str,
    vi_po: str,
    invoice_vendor: str,
    po_vendor: str,
    vi_vendor: str,
    invoice_number: str,
    po_invoice_number: str,
    vi_invoice_number: str,
    invoice_amount: float | None,
    po_amount: float | None,
    vi_amount: float | None,
    invoice_date: str,
    po_date: str,
    vi_date: str,
) -> dict[str, Any]:
    """Dashboard three-way match: uploaded invoice vs Tekion PO vs Pre-invoice."""
    return {
        "poNumber": {
            "invoice": invoice_po,
            "po": po_po,
            "preInvoice": vi_po,
        },
        "vendor": {
            "invoice": invoice_vendor,
            "po": po_vendor,
            "preInvoice": vi_vendor,
        },
        "invoiceNumber": {
            "invoice": invoice_number,
            "po": po_invoice_number,
            "preInvoice": vi_invoice_number,
        },
        "amount": {
            "invoice": float(invoice_amount) if invoice_amount is not None else None,
            "po": po_amount,
            "preInvoice": vi_amount,
        },
        "invoiceDate": {
            "invoice": invoice_date,
            "po": po_date,
            "preInvoice": vi_date,
        },
    }


def verify_vendor_invoice(
    client: TekionApiClient,
    expected: ViVerifyInput,
    *,
    dry_run: bool = True,
) -> ViVerifyResult:
    """Compare OCR to the Tekion VI; optionally attach PDF and complete VI."""
    result = ViVerifyResult()
    svc = ApApprovalService(client)

    if expected.dealership_name:
        dealer_id = client.find_dealer_by_name(expected.dealership_name)
        if not dealer_id:
            result.code = CODE_TEKION
            result.message = f"Could not match dealership '{expected.dealership_name}'"
            return result
        client.switch_dealer(dealer_id)

    ocr_po_number = str(expected.po_number or "").strip()
    invoice_number = _norm_invoice(expected.invoice_number)
    ocr_date = _parse_date(expected.invoice_date)
    result.invoice_date = _fmt_date(ocr_date) or str(expected.invoice_date or "").strip()

    if not invoice_number:
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = (
            "Invoice number is missing on the document. It must match the Invoice "
            "Number on Tekion's Vendor Invoice screen exactly."
        )
        return result
    if expected.invoice_amount is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            "Invoice amount is missing on the document. It must match Invoice "
            "Amount on Tekion's Vendor Invoice screen to the cent."
        )
        return result

    # ── Primary: find Vendor Invoice by invoice number ───────────────────────
    hit = find_pre_invoice_by_invoice_number(svc, invoice_number)
    if not hit:
        result.code = CODE_PRE_INVOICE_MISSING
        result.message = (
            f"No Vendor Invoice (Pre Invoice) found in Tekion for invoice "
            f"{invoice_number!r}. Create the PO, pre-invoice it with this "
            "invoice number, then upload again."
        )
        return result

    result.invoice_id = str(hit.get("id") or "")
    vi_invoice_number = _norm_invoice(hit.get("invoiceNumber") or invoice_number)
    result.invoice_number = vi_invoice_number
    vi_amount = _money(hit.get("invoiceAmount"))
    result.tekion_amount = vi_amount
    vi_vendor = str(
        hit.get("payeeName")
        or (hit.get("vendorDetails") or {}).get("vendorName")
        or ""
    ).strip()
    vi_po = _po_num_from_vi_hit(hit)
    vi_date = _parse_date(hit.get("invoiceDate"))
    result.tekion_invoice_date = _fmt_date(vi_date)

    if not result.invoice_id:
        result.code = CODE_PRE_INVOICE_MISSING
        result.message = (
            f"Tekion returned invoice {invoice_number!r} without an id — cannot "
            "load postings. Open the Vendor Invoice in Tekion and try again."
        )
        return result

    # Load PO (dealership-scoped) for the three-way legs.
    lookup_po = vi_po or ocr_po_number
    po: dict[str, Any] | None = None
    if lookup_po:
        po = client.find_purchase_order(lookup_po)

    po_po = str((po or {}).get("orderNumber") or "").strip() if po else ""
    po_vendor = str(
        ((po or {}).get("vendorDetails") or {}).get("vendorName") or ""
    ).strip()
    po_amount = _money((po or {}).get("totalAmount")) if po else None
    po_invoice_list = _invoice_numbers_on_po(po)
    # Display: the matching number on the PO, else all listed.
    matched_on_po = next(
        (
            n
            for n in po_invoice_list
            if _invoice_numbers_equal(n, invoice_number)
        ),
        "",
    )
    po_invoice_display = matched_on_po or ", ".join(po_invoice_list)
    po_date = _po_date_for_invoice(po, invoice_number)

    result.po_number = po_po or vi_po or ocr_po_number
    result.po_total = po_amount
    result.vendor_name = vi_vendor or po_vendor or None

    ocr_vendor = str(expected.vendor_name or "").strip()
    invoice_amt = float(expected.invoice_amount)
    invoice_date_s = result.invoice_date or _fmt_date(ocr_date) or ""

    def _snap() -> dict[str, Any]:
        return _build_match_snapshot(
            invoice_po=ocr_po_number,
            po_po=po_po,
            vi_po=vi_po,
            invoice_vendor=ocr_vendor,
            po_vendor=po_vendor,
            vi_vendor=vi_vendor,
            invoice_number=invoice_number,
            po_invoice_number=po_invoice_display,
            vi_invoice_number=vi_invoice_number,
            invoice_amount=invoice_amt,
            po_amount=po_amount,
            vi_amount=vi_amount,
            invoice_date=invoice_date_s,
            po_date=_fmt_date(po_date),
            vi_date=_fmt_date(vi_date),
        )

    # Keep match snapshot as we go so failures still show the panel.
    result.match = _snap()

    ap_expected = ExpectedInvoice(
        invoice_number=invoice_number,
        po_number=result.po_number or ocr_po_number,
        invoice_amount=invoice_amt,
        reference_number=expected.reference_number or invoice_number,
        ro_number=expected.ro_number,
        po_type=expected.po_type,
        amount_tolerance=0.0,  # Loom: exact match, no tolerance
    )

    # ── 1. PO Number (invoice ↔ PO ↔ Pre-invoice) ───────────────────────────
    if not vi_po:
        result.code = CODE_PO_NUMBER_MISMATCH
        result.message = (
            f"Vendor Invoice {invoice_number!r} in Tekion has no linked PO number. "
            "Link the PO on the Pre Invoice screen, then re-run checks."
        )
        result.match = _snap()
        return result
    if not po or not po_po:
        result.code = CODE_PO_NOT_FOUND
        result.message = (
            f"PO {vi_po} linked on the Vendor Invoice was not found in Tekion at "
            "this dealership. Nothing was verified."
        )
        result.match = _snap()
        return result
    if not _po_nums_equal(vi_po, po_po):
        result.code = CODE_PO_NUMBER_MISMATCH
        result.message = (
            f"PO number on the Pre Invoice is {vi_po!r}, but the Tekion PO record "
            f"is {po_po!r}. They must match. Fix the link in Tekion, then re-run."
        )
        result.match = _snap()
        return result
    if ocr_po_number and not _po_nums_equal(ocr_po_number, vi_po):
        result.code = CODE_PO_NUMBER_MISMATCH
        result.message = (
            f"PO number on the document is {ocr_po_number!r}, but Tekion Pre Invoice "
            f"/ PO has {vi_po!r}. All three must match. Correct the PO in Tekion or "
            "re-upload a clearer scan, then re-run checks."
        )
        result.match = _snap()
        return result

    # ── 2. Vendor Number / Name (invoice ↔ PO ↔ Pre-invoice) ─────────────────
    if not vi_vendor:
        result.code = CODE_VENDOR_MISMATCH
        result.message = (
            "Tekion Vendor Invoice has no Vendor Number / Name. Fill Vendor on the "
            "Pre Invoice screen, then re-run checks."
        )
        result.match = _snap()
        return result
    if not po_vendor:
        result.code = CODE_VENDOR_MISMATCH
        result.message = (
            f"Tekion PO {po_po} has no vendor name. Fix the vendor on the PO, then "
            "re-run checks."
        )
        result.match = _snap()
        return result
    if not _names_match(vi_vendor, po_vendor):
        result.code = CODE_VENDOR_MISMATCH
        result.message = (
            f"Vendor on the Pre Invoice is {vi_vendor!r}, but the PO has "
            f"{po_vendor!r}. They must match Vendor Number / Name. Correct the "
            "vendor in Tekion, then re-run checks."
        )
        result.match = _snap()
        return result
    if ocr_vendor and not _names_match(ocr_vendor, vi_vendor):
        result.code = CODE_VENDOR_MISMATCH
        result.message = (
            f"Vendor on the invoice is {ocr_vendor!r}, but Tekion PO / Pre Invoice "
            f"has {vi_vendor!r}. All three must match. Correct the vendor (or the "
            "PO) in Tekion, then re-run checks."
        )
        result.match = _snap()
        return result

    # ── 3. Full Invoice Number (invoice ↔ PO ↔ Pre-invoice) ──────────────────
    if not _invoice_numbers_equal(vi_invoice_number, invoice_number):
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = (
            f"Invoice number on the document is {invoice_number!r}, but Tekion "
            f"Pre Invoice has {vi_invoice_number!r}. They must match completely "
            "(including any prefix or suffix)."
        )
        result.match = _snap()
        return result
    if not po_invoice_list:
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = (
            f"Tekion PO {po_po} does not list any invoice numbers. Pre-invoice "
            f"with {invoice_number!r} on that PO, then re-run checks."
        )
        result.match = _snap()
        return result
    if not any(_invoice_numbers_equal(n, invoice_number) for n in po_invoice_list):
        listed = ", ".join(repr(n) for n in po_invoice_list)
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = (
            f"Invoice number {invoice_number!r} is not on Tekion PO {po_po} "
            f"(PO has: {listed}). The PO, Pre Invoice, and document must carry "
            "the same full invoice number."
        )
        result.match = _snap()
        return result

    # ── Near-duplicates on Tekion's Pre Invoice list (BLK123 vs 123, …RI) ──
    dup = _find_tekion_near_duplicates(svc, invoice_number, result.invoice_id)
    if dup:
        others = ", ".join(repr(x) for x in dup)
        result.code = CODE_DUPLICATE
        result.message = (
            f"Tekion already has another Pre Invoice whose number looks like the "
            f"same invoice ({invoice_number!r}): {others}. Letter prefixes or "
            "suffixes may have been dropped when the PO was typed. Verify in "
            "Tekion and remove the duplicate before this one is posted."
        )
        result.match = _snap()
        return result

    # ── 4. Invoice Amount exact cents (invoice ↔ PO ↔ Pre-invoice) ───────────
    if vi_amount is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            "Tekion did not return an Invoice Amount on the Pre Invoice. Open the "
            "Vendor Invoice in Tekion and confirm Invoice Amount is filled in."
        )
        result.match = _snap()
        return result
    if po_amount is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            f"Tekion PO {po_po} has no total amount to compare. Fix the PO total "
            "in Tekion, then re-run checks."
        )
        result.match = _snap()
        return result
    if not _money_eq(invoice_amt, vi_amount):
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            f"Invoice amount on the document is ${invoice_amt:,.2f}, but Tekion "
            f"Pre Invoice Amount is ${float(vi_amount):,.2f}. Uploaded invoice, "
            "PO total, and Pre Invoice amount must match to the cent."
        )
        result.match = _snap()
        return result
    if not _money_eq(invoice_amt, po_amount) or not _money_eq(vi_amount, po_amount):
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            f"Amounts do not match across all three: document ${invoice_amt:,.2f}, "
            f"Tekion PO total ${float(po_amount):,.2f}, Pre Invoice "
            f"${float(vi_amount):,.2f}. Correct the amount in Tekion (or the PO), "
            "then re-run checks."
        )
        result.match = _snap()
        return result

    # ── 5. Invoice Date (invoice ↔ Pre-invoice; PO when Tekion provides it) ──
    if not ocr_date:
        result.code = CODE_DATE_MISMATCH
        result.message = (
            "Invoice date is missing on the document. It must match Invoice Date "
            "on Tekion's Vendor Invoice screen."
        )
        result.match = _snap()
        return result
    if vi_date is None:
        result.code = CODE_DATE_MISMATCH
        result.message = (
            f"Invoice date on the document is {_fmt_date(ocr_date)}, but Tekion "
            "has no Invoice Date on the Vendor Invoice. Enter the date in Tekion, "
            "then re-run checks."
        )
        result.match = _snap()
        return result
    if ocr_date != vi_date:
        result.code = CODE_DATE_MISMATCH
        result.message = (
            f"Invoice date on the document is {_fmt_date(ocr_date)}, but Tekion "
            f"Pre Invoice Date is {_fmt_date(vi_date)}. They must match. Update "
            "Invoice Date in Tekion, then re-run."
        )
        result.match = _snap()
        return result
    if po_date is not None and po_date != ocr_date:
        result.code = CODE_DATE_MISMATCH
        result.message = (
            f"Invoice date on the document / Pre Invoice is {_fmt_date(ocr_date)}, "
            f"but the date on Tekion PO {po_po} for this invoice is "
            f"{_fmt_date(po_date)}. All three must match."
        )
        result.match = _snap()
        return result

    # ── Postings / GL ────────────────────────────────────────────────────────
    try:
        result.postings = svc.get_invoice_postings(result.invoice_id or "")
    except Exception as exc:  # noqa: BLE001
        result.code = CODE_TEKION
        result.message = f"Could not load Tekion invoice postings: {exc}"
        return result

    expense = _expense_postings(result.postings)
    if not expense:
        result.code = CODE_NO_GL
        result.message = (
            "Tekion's Vendor Invoice has no expense GL lines. Add the GL accounts "
            "on the pre-invoice / Journal Entry in Tekion, then run this again."
        )
        return result

    missing_gl = [p for p in expense if not _account_number(p)]
    if missing_gl:
        result.code = CODE_NO_GL
        result.message = (
            "Tekion's Vendor Invoice is missing a GL account on one or more expense "
            "lines. Fix the GL on the Journal Entry screen in Tekion, then run again."
        )
        return result

    # ── JE screen: Balance must be $0.00 (PO_INVOICE / Misc / Sublet / Stock) ─
    credit_total, debit_total, balance = _postings_balance(result.postings)
    if abs(balance) > _BALANCE_TOLERANCE:
        result.code = CODE_UNBALANCED
        result.message = (
            f"Tekion Journal Entry does not balance: Debit ${debit_total:,.2f}, "
            f"Credit ${credit_total:,.2f}, Balance ${balance:,.2f}. On the JE "
            "screen Balance must be $0.00 before this invoice can pass. Fix the "
            "GL lines in Tekion, then re-run checks."
        )
        result.match = _snap()
        result.notes.append(
            f"JE Balance ${balance:,.2f} (debit ${debit_total:,.2f} / "
            f"credit ${credit_total:,.2f})"
        )
        return result

    # ── Control column (JE): vendor name, or RO for Sublet GL 2460 ───────────
    # Temporarily off (ENFORCE_JE_CONTROL=False). Flip the flag to re-enable.
    if ENFORCE_JE_CONTROL:
        po_vendor_for_control = result.vendor_name or expected.vendor_name or ""
        ro = str(expected.ro_number or "").strip()

        for p in expense:
            acct = _account_number(p)
            control = str(p.get("refText") or "").strip()
            description = str(p.get("description") or "").strip()

            if acct == SUBLET_GL:
                if not ro:
                    result.code = CODE_JE_CONTROL
                    result.message = (
                        f"GL {SUBLET_GL} (Sublet) must be controlled by the Repair Order "
                        "number, but no RO # was read from the invoice. Enter the RO on "
                        "the document / Tekion Control column, then re-run."
                    )
                    result.match = _snap()
                    return result
                if _norm_name(control) != _norm_name(ro):
                    result.code = CODE_JE_CONTROL
                    result.message = (
                        f"GL {SUBLET_GL} Control is {control!r}, but Sublet lines must use "
                        f"the Repair Order number {ro!r} in the Control column on Tekion's "
                        "Journal Entry. Set Control to the RO #, then re-run checks."
                    )
                    result.match = _snap()
                    return result
                continue

            # All other expense GLs: Control = PO vendor name.
            if not control:
                result.code = CODE_JE_CONTROL
                result.message = (
                    f"GL {acct} has an empty Control on Tekion's Journal Entry. "
                    f"Set Control to the PO vendor name {po_vendor_for_control!r}, "
                    "then re-run checks."
                )
                result.match = _snap()
                return result
            if po_vendor_for_control and not _names_match(control, po_vendor_for_control):
                result.code = CODE_JE_CONTROL
                result.message = (
                    f"GL {acct} Control is {control!r}, but it must be the PO vendor name "
                    f"{po_vendor_for_control!r} (Journal Entry → Control column). "
                    "Update Control in Tekion, then re-run checks."
                )
                result.match = _snap()
                return result

            # 7xxx still need a description for AP.
            if acct.startswith("7") and not description:
                result.code = CODE_JE_CONTROL
                result.message = (
                    f"GL {acct} starts with 7 and needs a Description on Tekion's "
                    "Journal Entry (Control already checked). Add the description, "
                    "then re-run checks."
                )
                result.match = _snap()
                return result
    else:
        result.notes.append(
            "JE Control checks skipped (ENFORCE_JE_CONTROL=False) — re-enable later."
        )

    result.ok = True
    result.match = _snap()
    result.notes.append("Verification passed against Tekion Vendor Invoice.")

    if dry_run:
        result.notes.append("Dry run — skipped attach and VI complete.")
        return result

    # ── Pass path: attach PDF + complete VI ──────────────────────────────────
    try:
        _complete_vendor_invoice(client, svc, expected, result)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.code = CODE_TEKION
        result.message = str(exc)
        result.completed = False
    return result


def _find_tekion_near_duplicates(
    svc: ApApprovalService,
    invoice_number: str,
    matched_invoice_id: str | None,
) -> list[str]:
    """Other PRE_INVOICED invoice numbers that share this digit core."""
    core = invoice_digits(invoice_number)
    if not core:
        return []
    our_id = str(matched_invoice_id or "")
    found: list[str] = []
    seen: set[str] = set()
    for hit in svc.search_pre_invoiced(search_text=core, rows=50):
        other_id = str(hit.get("id") or "")
        other_num = str(hit.get("invoiceNumber") or "").strip()
        if not other_num:
            continue
        if our_id and other_id == our_id:
            continue
        key = normalize_invoice(other_num)
        if key in seen:
            continue
        if invoices_are_near_duplicates(invoice_number, other_num):
            seen.add(key)
            found.append(other_num)
    return found


def _complete_vendor_invoice(
    client: TekionApiClient,
    svc: ApApprovalService,
    expected: ViVerifyInput,
    result: ViVerifyResult,
) -> None:
    """Attach the invoice PDF and finish the VI / AP posting in Tekion."""
    media_id: str | None = None
    if expected.invoice_file_path:
        media_id = client.upload_document(
            expected.invoice_file_path,
            display_name=expected.invoice_file_name or None,
        )
        result.media_id = media_id
        result.notes.append(f"Uploaded invoice mediaId={media_id}")

    ap_expected = ExpectedInvoice(
        invoice_number=_norm_invoice(expected.invoice_number),
        po_number=str(result.po_number or expected.po_number),
        invoice_amount=float(expected.invoice_amount),
        reference_number=expected.reference_number or expected.invoice_number,
        ro_number=expected.ro_number,
        po_type=expected.po_type,
        amount_tolerance=0.0,
    )
    prepared = svc.prepare_postings(result.postings, ap_expected)
    # Ensure expense Control = vendor (or RO for 2460) before complete.
    vendor = result.vendor_name or expected.vendor_name or ""
    for line in prepared:
        if (line.get("metaData") or {}).get("apPostingLine"):
            continue
        acct = _account_number(line)
        if acct == SUBLET_GL and expected.ro_number:
            line["refText"] = expected.ro_number
        elif vendor:
            line["refText"] = vendor
    attachments = [{"mediaId": media_id}] if media_id else []

    try:
        svc.post_transaction(
            result.invoice_id or "",
            prepared,
            attachments=attachments,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Verification passed and the invoice file was "
            f"{'uploaded' if media_id else 'not uploaded'}, but completing the "
            "Vendor Invoice in Tekion failed. Re-capture Post Transaction with "
            f"`npm run pw:capture:ap`. Last error: {exc}"
        ) from exc

    result.completed = True
    result.notes.append("Completed Vendor Invoice (attach + post transaction).")
