"""Verify an uploaded invoice against an already pre-invoiced Vendor Invoice.

Parts raise the PO and pre-invoice in Tekion first. This module does the last
step only:

  1. Find the PO by number (dealership-scoped).
  2. Require PRE_INVOICED (or find the VI in the Pre Invoice list).
  3. Compare OCR vs Tekion: amount (exact), invoice number (exact), expense GLs.
  4. For expense GLs starting with ``7``, require control + description.
  5. On pass (and writes on): attach the PDF and complete the VI / AP posting.

Never creates a PO. Never calls preInvoice/post.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
CODE_NO_GL = "NO_GL_ACCOUNT_WRITTEN"
CODE_JE_CONTROL = "JE_CONTROL_REQUIRED"
CODE_DUPLICATE = "DUPLICATE_INVOICE"
CODE_TEKION = "TEKION_ERROR"


@dataclass
class ViVerifyInput:
    po_number: str
    invoice_number: str
    invoice_amount: float
    dealership_name: str | None = None
    po_type: str = "MISCELLANEOUS"
    ro_number: str | None = None
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
    postings: list[dict[str, Any]] = field(default_factory=list)
    media_id: str | None = None
    completed: bool = False
    notes: list[str] = field(default_factory=list)

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

    po_number = str(expected.po_number or "").strip()
    invoice_number = _norm_invoice(expected.invoice_number)
    if not po_number:
        result.code = CODE_PO_NOT_FOUND
        result.message = "No purchase order number could be read from the invoice"
        return result
    if not invoice_number:
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = "No invoice number could be read from the invoice"
        return result
    if expected.invoice_amount is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = "No invoice total could be read from the invoice"
        return result

    # ── PO must exist ────────────────────────────────────────────────────────
    po = client.find_purchase_order(po_number)
    if not po:
        result.code = CODE_PO_NOT_FOUND
        result.message = (
            f"PO {po_number} was not found in Tekion at this dealership. "
            "Nothing was posted."
        )
        return result

    result.po_number = str(po.get("orderNumber") or po_number)
    result.po_total = _money(po.get("totalAmount"))
    result.vendor_name = (po.get("vendorDetails") or {}).get("vendorName")

    invoice_status = str(po.get("invoiceStatus") or "").upper()
    # ── Must already be pre-invoiced ─────────────────────────────────────────
    if invoice_status in ("", "NOT_PRE_INVOICED"):
        result.code = CODE_PRE_INVOICE_MISSING
        result.message = (
            f"PO {result.po_number} exists in Tekion but is not pre-invoiced yet "
            f"(status={invoice_status or 'NOT_PRE_INVOICED'}). Pre-invoice it in "
            "Tekion first, then upload this invoice again."
        )
        return result

    # ── Find the VI in the Pre Invoice / AP list ─────────────────────────────
    ap_expected = ExpectedInvoice(
        invoice_number=invoice_number,
        po_number=result.po_number,
        invoice_amount=float(expected.invoice_amount),
        reference_number=expected.reference_number or invoice_number,
        ro_number=expected.ro_number,
        po_type=expected.po_type,
        amount_tolerance=0.0,  # Loom: exact match, no tolerance
    )
    hit = svc.find_invoice(ap_expected)
    if not hit:
        # PO may already be past PRE_INVOICED (e.g. PAYMENT_PENDING) — still try
        # matching invoice numbers on the PO record.
        on_po = [
            str(x.get("invoiceNumber") or "").strip()
            for x in (po.get("invoiceNumbers") or [])
            if isinstance(x, dict)
        ]
        if invoice_number not in on_po and _norm_invoice(invoice_number).lower() not in {
            _norm_invoice(n).lower() for n in on_po
        }:
            result.code = CODE_PRE_INVOICE_MISSING
            result.message = (
                f"No Vendor Invoice found in Tekion for invoice {invoice_number!r} "
                f"on PO {result.po_number}. Pre-invoice the PO in Tekion first."
            )
            return result
        # Invoice # is on the PO but not in PRE_INVOICED search — treat as
        # already moved along; still need postings via invoiceId on the PO.
        invoice_id = None
        for x in po.get("invoiceNumbers") or []:
            if isinstance(x, dict) and _norm_invoice(x.get("invoiceNumber")) == invoice_number:
                invoice_id = str(x.get("invoiceId") or "") or None
                break
        if not invoice_id:
            result.code = CODE_PRE_INVOICE_MISSING
            result.message = (
                f"PO {result.po_number} lists invoice {invoice_number!r} but Tekion "
                "did not return an invoice id to verify postings."
            )
            return result
        result.invoice_id = invoice_id
        result.invoice_number = invoice_number
        result.tekion_amount = result.po_total
    else:
        result.invoice_id = str(hit.get("id") or "")
        result.invoice_number = str(hit.get("invoiceNumber") or "")
        result.tekion_amount = _money(hit.get("invoiceAmount"))
        result.vendor_name = (
            hit.get("payeeName")
            or (hit.get("vendorDetails") or {}).get("vendorName")
            or result.vendor_name
        )

    # ── Exact invoice number ─────────────────────────────────────────────────
    if _norm_invoice(result.invoice_number) != invoice_number:
        # Also allow case-insensitive full-string match.
        if _norm_invoice(result.invoice_number).lower() != invoice_number.lower():
            result.code = CODE_INVOICE_NUMBER_MISMATCH
            result.message = (
                f"Invoice number on the document is {invoice_number!r}, but Tekion "
                f"has {result.invoice_number!r}. They must match completely "
                "(including any prefix or suffix)."
            )
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
        return result

    # ── Exact amount (OCR invoice total vs Tekion VI amount; else PO total) ──
    tekion_amt = result.tekion_amount
    if tekion_amt is None:
        tekion_amt = result.po_total
    if tekion_amt is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = "Tekion did not return an amount to compare against the invoice."
        return result
    if round(float(expected.invoice_amount), 2) != round(float(tekion_amt), 2):
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            f"Invoice total is ${float(expected.invoice_amount):,.2f}, but Tekion "
            f"has ${float(tekion_amt):,.2f}. Amounts must match exactly."
        )
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
            "on the pre-invoice in Tekion, then run this again."
        )
        return result

    missing_gl = [p for p in expense if not _account_number(p)]
    if missing_gl:
        result.code = CODE_NO_GL
        result.message = (
            "Tekion's Vendor Invoice is missing a GL account on one or more expense "
            "lines. Fix the GL on the pre-invoice in Tekion, then run this again."
        )
        return result

    # GL starting with 7 → control + description required (JE / AP exception).
    for p in expense:
        acct = _account_number(p)
        if not acct.startswith("7"):
            continue
        control = str(p.get("refText") or "").strip()
        description = str(p.get("description") or "").strip()
        if not control or not description:
            result.code = CODE_JE_CONTROL
            result.message = (
                f"GL account {acct} starts with 7 and needs both a control number "
                "and a description on the journal-entry / VI screen in Tekion. "
                "Add them there, then run this again."
            )
            return result

    result.ok = True
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
    # Search by digit core first (catches BLK123 when we have 123 and vice versa).
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
        # Same full number on another VI, or letter-prefix/suffix variant.
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
    """Attach the invoice PDF and finish the VI / AP posting in Tekion.

    Post Transaction was never fully captured in this repo. We upload the media,
    prepare postings (description / RO), then POST the best-known complete
    endpoint. If Tekion rejects the shape, the error surfaces for a re-capture.
    """
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
