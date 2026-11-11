"""Verify an uploaded invoice against an already pre-invoiced Vendor Invoice.

Parts raise the PO and pre-invoice in Tekion first. This module does the last
step only:

  1. Find the Vendor Invoice by **invoice number** on the Pre Invoice list
     (primary). OCR often has no PO #, or misreads a date as a PO #.
  2. Read the linked PO # from that VI; optionally confirm the PO still exists.
  3. Three-way compare (uploaded invoice == Tekion PO == Pre-invoice VI)
     for: PO #, Vendor, Invoice #, Invoice amount (exact cents). Invoice date
     is shown on the panel but is **not** a pass/fail gate — Parts often
     raise the PO days/weeks after the vendor invoice date.
  4. JE screen (postings): Balance must be $0.00; expense GL Control must
     equal the PO vendor name — except GL 2460 (Sublet) = RO #. For Sublet,
     the RO is taken from the Tekion PO (`subletDetails.assetNumber`) first;
     OCR is only a fallback. GLs starting with 7 also need a description
     (control checks behind ENFORCE_JE_CONTROL).
  5. On pass (and writes on): attach the PDF and complete the VI / AP posting.

Never creates a PO or a pre-invoice. preInvoice/post is only called with the
existing invoiceId, to attach the PDF -- that is how Tekion links a file.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from api.services.ap_approval import ApApprovalService, ExpectedInvoice, set_control
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
    # One-shot waive after dashboard "Ignore" on a Tekion near-duplicate hold.
    skip_near_duplicate_check: bool = False
    # Clerk Force push: VI+PO must still exist in Tekion, but OCR↔Tekion
    # vendor / invoice # / amount header mismatches are skipped so attach+post
    # can proceed. Tekion's own VI↔PO link and GL/balance checks still apply.
    force_vi_headers: bool = False
    # OCR / folder says this is a vendor credit (credit memo). Tekion stores the
    # Pre Invoice amount as negative while OCR totals stay positive — magnitude
    # match is enough; we never rebuild JE lines on the PO path.
    is_credit: bool = False


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
    # The Vendor Invoice's invoiceDate as Tekion holds it; posted as scheduledTime.
    tekion_invoice_date_ms: int | None = None
    # The Vendor Invoice record from poInvoice/search; re-posted to attach the PDF.
    vi_record: dict[str, Any] = field(default_factory=dict)
    postings: list[dict[str, Any]] = field(default_factory=list)
    media_id: str | None = None
    attached: bool = False
    completed: bool = False
    notes: list[str] = field(default_factory=list)
    # Snapshot for the dashboard three-way match panel.
    match: dict[str, Any] = field(default_factory=dict)
    # RO(s) for Sublet 2460 Control (Tekion PO preferred over OCR).
    # Single RO, or comma-separated when the Sublet PO lists several.
    resolved_ro: str | None = None
    # Ordered (ro, amount) rows from Tekion Sublet PO — used to map each
    # 2460 JE line to its own Repair Order Control.
    po_ro_rows: list[tuple[str, float]] = field(default_factory=list)

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


def _ro_amount_from_sublet_detail(detail: dict[str, Any]) -> float:
    for key in ("laborAmount", "amount", "partAmount"):
        raw = detail.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _ros_from_tekion_po(po: dict[str, Any] | None) -> list[tuple[str, float]]:
    """All Repair Orders on a Tekion Sublet PO, in Sublet List order.

    Each ``subletDetails`` row carries ``assetNumber`` (RO #) and an amount.
    Multi-RO POs (e.g. six cars on one vendor invoice) become six 2460 JE
    lines — Control must be that row's RO, not the first RO on every line.
    """
    if not po:
        return []
    rows: list[tuple[str, float]] = []
    for detail in po.get("subletDetails") or []:
        if not isinstance(detail, dict):
            continue
        ro = ""
        for key in ("assetNumber", "roNumber", "roNo", "repairOrderNumber"):
            val = str(detail.get(key) or "").strip()
            if val and val.lower() not in ("none", "null"):
                ro = val
                break
        if not ro:
            continue
        rows.append((ro, _ro_amount_from_sublet_detail(detail)))
    if rows:
        return rows
    for key in ("controlNumber", "assetNumber"):
        val = str(po.get(key) or "").strip()
        if val and val.lower() not in ("none", "null"):
            return [(val, float(_money(po.get("totalAmount")) or 0.0))]
    return []


def _ro_from_tekion_po(po: dict[str, Any] | None) -> str:
    """First RO on the Sublet PO (compat / single-RO path)."""
    rows = _ros_from_tekion_po(po)
    return rows[0][0] if rows else ""


def _format_ro_list(rows: list[tuple[str, float]]) -> str:
    """Stable comma-separated RO list for the document / UI (dedupe, keep order)."""
    seen: set[str] = set()
    out: list[str] = []
    for ro, _amt in rows:
        if ro in seen:
            continue
        seen.add(ro)
        out.append(ro)
    return ", ".join(out)


def _apply_sublet_2460_controls(
    prepared: list[dict[str, Any]],
    *,
    po_ro_rows: list[tuple[str, float]],
    fallback_ro: str,
) -> list[str]:
    """Set Control on each GL 2460 expense line to the matching Sublet RO.

    - If Tekion already filled Control (Parts did), leave it alone.
    - Else match an unused PO ``subletDetails`` row by amount (same order as
      the Sublet List / JE when amounts align).
    - Else fall back to the single ``fallback_ro`` when only one RO exists.
    Returns notes describing what was applied.
    """
    notes: list[str] = []
    unused = list(po_ro_rows)
    unique_ros = list(dict.fromkeys(ro for ro, _ in po_ro_rows))
    single_ro = (
        unique_ros[0]
        if len(unique_ros) == 1
        else (fallback_ro.strip() if fallback_ro.strip() and "," not in fallback_ro else "")
    )

    for line in prepared:
        if (line.get("metaData") or {}).get("apPostingLine"):
            continue
        if _account_number(line) != SUBLET_GL:
            continue
        existing = str(line.get("refText") or line.get("refId") or "").strip()
        if existing:
            # Keep Tekion's Control, but ensure both refId + refText (A217).
            set_control(line, existing)
            notes.append(f"GL {SUBLET_GL} Control already {existing!r} — left as-is.")
            # Consume a matching unused PO row so later lines don't steal it.
            for i, (ro, _amt) in enumerate(unused):
                if ro == existing:
                    unused.pop(i)
                    break
            continue

        line_amt = float(line.get("amount") or 0)
        chosen = ""
        for i, (ro, amt) in enumerate(unused):
            if abs(line_amt - amt) < 0.005:
                chosen = ro
                unused.pop(i)
                break
        if not chosen and single_ro:
            chosen = single_ro
        if chosen:
            set_control(line, chosen)
            notes.append(
                f"GL {SUBLET_GL} ${line_amt:,.2f} Control set to RO {chosen!r}."
            )
        else:
            notes.append(
                f"GL {SUBLET_GL} ${line_amt:,.2f} has no Control and no matching "
                "Sublet PO RO amount — left empty."
            )
    return notes


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
    """Vendor/control compare: upper, drop punctuation, collapse spaces.

    OCR often prints ``U.S. AutoForce`` while Tekion stores ``USAUTOFORCE``.
    Dots/hyphens/commas/etc. become spaces (then collapsed); ``&`` → ``AND``.
    """
    text = str(value or "").strip().upper().replace("&", " AND ")
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def _compact_name(value: str) -> str:
    """Alphanumeric-only key so spaced vs compacted Tekion names can equal."""
    return re.sub(r"[^A-Z0-9]", "", value)


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
    if ca == cb or ca in cb or cb in ca:
        return True
    # Equality only on compacted form (no substring) — avoids US AUTO ⊂ USAUTOFORCE.
    xa, xb = _compact_name(ca), _compact_name(cb)
    return bool(xa and xb and xa == xb)


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
        pass
    # Same vendor formats as OCR ("04 NOV 26") via shared normalizer.
    from api.services.ocr_helpers import _normalize_date

    normalized = _normalize_date(text)
    if normalized:
        try:
            return datetime.strptime(normalized, "%m/%d/%Y").date()
        except ValueError:
            return None
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


def _try_load_postings(result: "ViVerifyResult", svc: "ApApprovalService") -> None:
    """Best-effort JE lines so amount/date fails still show GL on the detail panel."""
    if result.postings or not result.invoice_id:
        return
    try:
        result.postings = svc.get_invoice_postings(result.invoice_id)
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"Could not preload Tekion postings: {exc}")


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


def _money_eq_mag(a: float | None, b: float | None) -> bool:
    """Same cents ignoring sign (credit VI −$22 vs OCR/PO +$22)."""
    if a is None or b is None:
        return False
    return round(abs(float(a)), 2) == round(abs(float(b)), 2)


def _credit_amount_case(
    *,
    expected: "ViVerifyInput",
    vi_amount: float | None,
    po: dict[str, Any] | None,
) -> bool:
    """Whether amount checks should compare magnitude only.

    Credit Pre Invoices in Tekion carry a negative Invoice Amount; PO totals and
    OCR ``get_total_amount`` stay positive. Signs already live on Tekion's JE
    lines — VI verify only needs the dollars to agree.
    """
    if expected.is_credit:
        return True
    if vi_amount is not None and float(vi_amount) < 0:
        return True
    order = str((po or {}).get("orderType") or "").strip().upper()
    if "CREDIT" in order:
        return True
    return False


def _amounts_agree(
    a: float | None,
    b: float | None,
    *,
    credit: bool,
) -> bool:
    if _money_eq(a, b):
        return True
    return bool(credit and _money_eq_mag(a, b))


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

    ocr_po_number = str(expected.po_number or "").strip()
    invoice_number = _norm_invoice(expected.invoice_number)
    ocr_date = _parse_date(expected.invoice_date)
    result.invoice_date = _fmt_date(ocr_date) or str(expected.invoice_date or "").strip()

    def _stash_ocr_only() -> None:
        """Keep PDF legs on the panel even when we never reach a Tekion VI."""
        amt = expected.invoice_amount
        result.match = _build_match_snapshot(
            invoice_po=ocr_po_number,
            po_po="",
            vi_po="",
            invoice_vendor=str(expected.vendor_name or "").strip(),
            po_vendor="",
            vi_vendor="",
            invoice_number=invoice_number,
            po_invoice_number="",
            vi_invoice_number="",
            invoice_amount=float(amt) if amt is not None else None,
            po_amount=None,
            vi_amount=None,
            invoice_date=result.invoice_date or "",
            po_date="",
            vi_date="",
        )

    if expected.dealership_name:
        dealer_id = client.find_dealer_by_name(expected.dealership_name)
        if not dealer_id:
            result.code = CODE_TEKION
            result.message = f"Could not match dealership '{expected.dealership_name}'"
            _stash_ocr_only()
            return result
        client.switch_dealer(dealer_id)

    if not invoice_number:
        result.code = CODE_INVOICE_NUMBER_MISMATCH
        result.message = (
            "Invoice number is missing on the document. It must match the Invoice "
            "Number on Tekion's Vendor Invoice screen exactly."
        )
        _stash_ocr_only()
        return result
    if expected.invoice_amount is None:
        result.code = CODE_AMOUNT_MISMATCH
        result.message = (
            "Invoice amount is missing on the document. It must match Invoice "
            "Amount on Tekion's Vendor Invoice screen to the cent."
        )
        _stash_ocr_only()
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
        # Still stash OCR legs so the three-way panel shows amount / date /
        # vendor from the PDF. PO + VI columns stay empty (no Tekion hit).
        invoice_amt = float(expected.invoice_amount)
        invoice_date_s = result.invoice_date or _fmt_date(ocr_date) or ""
        result.match = _build_match_snapshot(
            invoice_po=ocr_po_number,
            po_po="",
            vi_po="",
            invoice_vendor=str(expected.vendor_name or "").strip(),
            po_vendor="",
            vi_vendor="",
            invoice_number=invoice_number,
            po_invoice_number="",
            vi_invoice_number="",
            invoice_amount=invoice_amt,
            po_amount=None,
            vi_amount=None,
            invoice_date=invoice_date_s,
            po_date="",
            vi_date="",
        )
        return result

    result.invoice_id = str(hit.get("id") or "")
    result.vi_record = hit
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
    if isinstance(hit.get("invoiceDate"), (int, float)):
        result.tekion_invoice_date_ms = int(hit["invoiceDate"])

    if not result.invoice_id:
        result.code = CODE_PRE_INVOICE_MISSING
        result.message = (
            f"Tekion returned invoice {invoice_number!r} without an id — cannot "
            "load postings. Open the Vendor Invoice in Tekion and try again."
        )
        invoice_amt = float(expected.invoice_amount)
        invoice_date_s = result.invoice_date or _fmt_date(ocr_date) or ""
        result.match = _build_match_snapshot(
            invoice_po=ocr_po_number,
            po_po="",
            vi_po="",
            invoice_vendor=str(expected.vendor_name or "").strip(),
            po_vendor="",
            vi_vendor="",
            invoice_number=invoice_number,
            po_invoice_number="",
            vi_invoice_number="",
            invoice_amount=invoice_amt,
            po_amount=None,
            vi_amount=None,
            invoice_date=invoice_date_s,
            po_date="",
            vi_date="",
        )
        return result

    # Load PO (dealership-scoped) for the three-way legs.
    lookup_po = vi_po or ocr_po_number
    po: dict[str, Any] | None = None
    if lookup_po:
        po = client.find_purchase_order(lookup_po)

    # Sublet RO lives on subletDetails; some list hits omit it — re-fetch by
    # universalId when we still need an RO for Control.
    po_looks_sublet = (
        str(expected.po_type or "").strip().upper() == "SUBLET"
        or str((po or {}).get("orderType") or "").strip().upper() == "SUBLET"
        or str((po or {}).get("universalId") or "").upper().startswith("SUBLET%")
    )
    if po and po_looks_sublet and not _ro_from_tekion_po(po):
        uid = str(po.get("universalId") or "").strip()
        if not uid:
            for detail in hit.get("poDetails") or []:
                if isinstance(detail, dict) and detail.get("universalId"):
                    uid = str(detail["universalId"]).strip()
                    break
        if uid:
            richer = svc.get_po(uid)
            if richer:
                po = richer

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

    # Sublet Control: ROs from Tekion PO first (Parts already linked them), OCR
    # only if the PO has nothing. Multi-RO POs keep every assetNumber — each
    # 2460 JE line is mapped later in _complete_vendor_invoice.
    po_ro_rows = _ros_from_tekion_po(po)
    result.po_ro_rows = po_ro_rows
    po_ro_display = _format_ro_list(po_ro_rows)
    ocr_ro = str(expected.ro_number or "").strip()
    if po_ro_display:
        if ocr_ro and ocr_ro != po_ro_display and ocr_ro not in po_ro_display:
            result.notes.append(
                f"RO(s) from Tekion Sublet PO: {po_ro_display} "
                f"(OCR had {ocr_ro!r}); using PO."
            )
        else:
            result.notes.append(f"RO(s) from Tekion Sublet PO: {po_ro_display}.")
        expected = replace(expected, ro_number=po_ro_display)
        result.resolved_ro = po_ro_display
    elif ocr_ro:
        result.notes.append(f"RO {ocr_ro!r} from OCR (Tekion PO had no sublet RO).")
        result.resolved_ro = ocr_ro

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
    # Load JE lines early so AMOUNT_MISMATCH / date fails still stash GL for UI.
    _try_load_postings(result, svc)

    ap_expected = ExpectedInvoice(
        invoice_number=invoice_number,
        po_number=result.po_number or ocr_po_number,
        invoice_amount=invoice_amt,
        reference_number=expected.reference_number or invoice_number,
        ro_number=expected.ro_number,
        po_type=expected.po_type,
        amount_tolerance=0.0,  # Loom: exact match, no tolerance
    )

    # ── 1. PO Number (Tekion PO ↔ Pre-invoice only; OCR PO is display) ───────
    # Customer PDF often prints a vendor "order #" / shop label that is not the
    # Tekion PO. Hard-fail only when Tekion's PO record and VI link disagree.
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
        # Keep going — three-way UI still shows the OCR value for humans.
        result.notes.append(
            f"OCR PO {ocr_po_number!r} differs from Tekion PO/VI {vi_po!r}; "
            "PO # check is Tekion PO ↔ Pre Invoice only."
        )
        result.match = _snap()

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
        if expected.force_vi_headers:
            result.notes.append(
                f"Force push: skipped vendor mismatch (invoice {ocr_vendor!r} vs "
                f"Tekion {vi_vendor!r}); using Tekion vendor."
            )
            result.match = _snap()
        else:
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
        if expected.force_vi_headers:
            result.notes.append(
                f"Force push: skipped invoice # mismatch (document "
                f"{invoice_number!r} vs Pre Invoice {vi_invoice_number!r}); "
                "using Tekion Pre Invoice number."
            )
            invoice_number = vi_invoice_number
            result.invoice_number = vi_invoice_number
            result.match = _snap()
        else:
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
        if expected.force_vi_headers:
            result.notes.append(
                f"Force push: invoice {invoice_number!r} not listed on Tekion PO "
                f"{po_po}; continuing with Pre Invoice link."
            )
            result.match = _snap()
        else:
            listed = ", ".join(repr(n) for n in po_invoice_list)
            result.code = CODE_INVOICE_NUMBER_MISMATCH
            result.message = (
                f"Invoice number {invoice_number!r} is not on Tekion PO {po_po} "
                f"(PO has: {listed}). The PO, Pre Invoice, and document must carry "
                "the same full invoice number."
            )
            result.match = _snap()
            return result

    # ── Near-duplicates on Tekion's Pre Invoice list (Daisy check) ─────────
    # Exact invoice # already matched above. Also scan for pattern variants
    # (BLK123 vs 123). Only hold as duplicate when that other Pre Invoice
    # shares this VI's PO number OR total amount — same rule Parts use on
    # the Pre Invoice screen.
    if not expected.skip_near_duplicate_check:
        dup = _find_confirmed_tekion_near_duplicates(
            svc,
            invoice_number,
            result.invoice_id,
            matched_po=vi_po,
            matched_amount=vi_amount,
        )
        if dup:
            others = ", ".join(repr(x) for x in dup)
            result.code = CODE_DUPLICATE
            result.message = (
                f"Tekion already has another Pre Invoice whose number looks like "
                f"the same invoice ({invoice_number!r}): {others}. That row "
                "shares this invoice's PO number or total amount, so it may be "
                "the same bill typed with letters dropped or added. Discard this "
                "upload, or Ignore to continue anyway."
            )
            result.match = _snap()
            return result

    # ── 4. Invoice Amount exact cents (invoice ↔ PO ↔ Pre-invoice) ───────────
    # Credit memos: Tekion VI amount is negative; OCR/PO stay positive. Compare
    # magnitude only — JE debit/credit direction already comes from Tekion.
    credit_amt = _credit_amount_case(
        expected=expected, vi_amount=vi_amount, po=po
    )
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
    if not _amounts_agree(invoice_amt, vi_amount, credit=credit_amt):
        if expected.force_vi_headers:
            result.notes.append(
                f"Force push: skipped amount mismatch (document "
                f"${invoice_amt:,.2f} vs Pre Invoice ${float(vi_amount):,.2f}); "
                "using Tekion Pre Invoice amount."
            )
            invoice_amt = float(vi_amount)
            result.match = _snap()
        else:
            result.code = CODE_AMOUNT_MISMATCH
            result.message = (
                f"Invoice amount on the document is ${invoice_amt:,.2f}, but Tekion "
                f"Pre Invoice Amount is ${float(vi_amount):,.2f}. Uploaded invoice, "
                "PO total, and Pre Invoice amount must match to the cent."
            )
            result.match = _snap()
            return result
    elif credit_amt and not _money_eq(invoice_amt, vi_amount):
        result.notes.append(
            f"Credit amount: document ${invoice_amt:,.2f} matches Pre Invoice "
            f"${float(vi_amount):,.2f} by magnitude (sign differs on credit VIs)."
        )
    if not (
        _amounts_agree(invoice_amt, po_amount, credit=credit_amt)
        and _amounts_agree(vi_amount, po_amount, credit=credit_amt)
    ):
        if expected.force_vi_headers:
            result.notes.append(
                f"Force push: skipped three-way amount mismatch (document "
                f"${float(expected.invoice_amount):,.2f}, PO "
                f"${float(po_amount):,.2f}, Pre Invoice ${float(vi_amount):,.2f})."
            )
            result.match = _snap()
        else:
            result.code = CODE_AMOUNT_MISMATCH
            result.message = (
                f"Amounts do not match across all three: document ${invoice_amt:,.2f}, "
                f"Tekion PO total ${float(po_amount):,.2f}, Pre Invoice "
                f"${float(vi_amount):,.2f}. Correct the amount in Tekion (or the PO), "
                "then re-run checks."
            )
            result.match = _snap()
            return result

    # ── 5. Invoice Date — informational only (not a pass/fail gate) ──────────
    # Parts routinely create / pre-invoice the PO days or weeks after the
    # vendor invoice date (Kevin / Daisy Nov 2026). Keep dates on the three-way
    # panel via _snap(), but never refuse on mismatch or missing date.
    if ocr_date and vi_date and ocr_date != vi_date:
        result.notes.append(
            f"Invoice date on document {_fmt_date(ocr_date)} differs from Tekion "
            f"Pre Invoice {_fmt_date(vi_date)} — date is not required to pass."
        )
    elif ocr_date and po_date is not None and po_date != ocr_date:
        result.notes.append(
            f"Invoice date on document {_fmt_date(ocr_date)} differs from Tekion "
            f"PO {po_po} date {_fmt_date(po_date)} — date is not required to pass."
        )
    elif not ocr_date:
        result.notes.append(
            "Invoice date missing on document — date is not required to pass."
        )

    # ── Postings / GL ────────────────────────────────────────────────────────
    # Prefer the early preload; retry once if that failed so GL checks still run.
    if not result.postings:
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

    # ── Sublet + GL 2460: RO is mandatory (always on — blocks attach/post) ───
    # Misc Control / vendor-name checks stay behind ENFORCE_JE_CONTROL.
    po_is_sublet = str(expected.po_type or "").strip().upper() == "SUBLET"
    has_sublet_gl = any(_account_number(p) == SUBLET_GL for p in expense)
    ro = str(expected.ro_number or "").strip()
    tekion_2460_control = any(
        _account_number(p) == SUBLET_GL and str(p.get("refText") or "").strip()
        for p in expense
    )
    if po_is_sublet and has_sublet_gl and not ro and not tekion_2460_control:
        result.code = CODE_JE_CONTROL
        result.message = (
            f"Sublet with GL {SUBLET_GL} requires a Repair Order number in Control, "
            "but no RO # was found on the invoice or on Tekion's Journal Entry. "
            "Enter the RO # (Correct & re-run or Tekion Control), then re-run. "
            "Nothing was posted."
        )
        result.match = _snap()
        return result

    # ── Control column (JE): vendor name, or RO for Sublet GL 2460 ───────────
    # Temporarily off (ENFORCE_JE_CONTROL=False). Flip the flag to re-enable.
    if ENFORCE_JE_CONTROL:
        po_vendor_for_control = result.vendor_name or expected.vendor_name or ""

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
    except DuplicateInvoiceNumber as exc:
        result.ok = False
        result.code = CODE_DUPLICATE
        result.message = str(exc)
        result.completed = False
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.code = CODE_TEKION
        result.message = str(exc)
        result.completed = False
    return result


def _pre_invoice_hits_for_near_dup_scan(
    svc: ApApprovalService, invoice_number: str
) -> list[dict[str, Any]]:
    """Candidate Pre Invoices for near-duplicate comparison.

    Tekion's search box often returns nothing for a short digit core like
    ``001`` even when ``BLK001`` / ``TEST-MUQSIT-001`` exist. Search the core,
    a leading-zero-stripped core, and an unfiltered recent Pre Invoice page,
    then dedupe by id.
    """
    core = invoice_digits(invoice_number)
    needles: list[str] = []
    if core:
        needles.append(core)
        stripped = core.lstrip("0")
        if stripped and stripped != core:
            needles.append(stripped)
    needles.append("")  # recent list — catches short cores Tekion search misses

    by_id: dict[str, dict[str, Any]] = {}
    for needle in needles:
        for hit in svc.search_pre_invoiced(search_text=needle, rows=50):
            hid = str(hit.get("id") or "") or normalize_invoice(
                str(hit.get("invoiceNumber") or "")
            )
            if not hid:
                continue
            by_id[hid] = hit
    return list(by_id.values())


def _find_confirmed_tekion_near_duplicates(
    svc: ApApprovalService,
    invoice_number: str,
    matched_invoice_id: str | None,
    *,
    matched_po: str,
    matched_amount: float | None,
) -> list[str]:
    """Other Pre Invoices that look like the same # and share PO or amount.

    Pattern-only hits (same digits, different letters) are not enough — Parts
    also confirm against the exact-matched row's PO number or total.
    """
    core = invoice_digits(invoice_number)
    if not core:
        return []
    our_id = str(matched_invoice_id or "")
    found: list[str] = []
    seen: set[str] = set()
    for hit in _pre_invoice_hits_for_near_dup_scan(svc, invoice_number):
        other_id = str(hit.get("id") or "")
        other_num = str(hit.get("invoiceNumber") or "").strip()
        if not other_num:
            continue
        if our_id and other_id == our_id:
            continue
        if _invoice_numbers_equal(other_num, invoice_number):
            continue
        key = normalize_invoice(other_num)
        if key in seen:
            continue
        if not invoices_are_near_duplicates(invoice_number, other_num):
            continue
        other_po = _po_num_from_vi_hit(hit)
        other_amt = _money(hit.get("invoiceAmount"))
        po_same = bool(matched_po and other_po and _po_nums_equal(matched_po, other_po))
        amt_same = _money_eq(matched_amount, other_amt)
        if not (po_same or amt_same):
            continue
        seen.add(key)
        found.append(other_num)
    return found


class DuplicateInvoiceNumber(RuntimeError):
    """Tekion refused the post: the invoice number is already posted (A111)."""


def _complete_vendor_invoice(
    client: TekionApiClient,
    svc: ApApprovalService,
    expected: ViVerifyInput,
    result: ViVerifyResult,
) -> None:
    """Attach the invoice PDF and finish the VI / AP posting in Tekion."""
    # Attach first: the re-post only works while the invoice is PRE_INVOICED.
    # A failure here stops before posting, so a re-run can finish the job.
    if expected.invoice_file_path:
        media_id = client.upload_document(
            expected.invoice_file_path,
            display_name=expected.invoice_file_name or None,
            module_name="ACCOUNTING",
            service_name="Vendor Invoices",
        )
        result.media_id = media_id
        result.notes.append(f"Uploaded invoice mediaId={media_id}")
        svc.attach_to_invoice(result.vi_record, media_id)
        result.attached = True
        result.notes.append(f"Attached mediaId={media_id} to the Vendor Invoice.")

    ap_expected = ExpectedInvoice(
        invoice_number=_norm_invoice(expected.invoice_number),
        po_number=str(result.po_number or expected.po_number),
        invoice_amount=float(expected.invoice_amount),
        reference_number=expected.reference_number or expected.invoice_number,
        ro_number=expected.ro_number,
        po_type=expected.po_type,
        amount_tolerance=0.0,
    )
    # prepare_postings fills description; 2460 Control is applied below so a
    # multi-RO Sublet PO does not stamp the first RO onto every JE line.
    prepared = svc.prepare_postings(result.postings, ap_expected)
    vendor = result.vendor_name or expected.vendor_name or ""
    fallback_ro = str(expected.ro_number or result.resolved_ro or "").strip()
    # One RO only in fallback (comma list is display, not a Control value).
    if "," in fallback_ro:
        fallback_ro = fallback_ro.split(",", 1)[0].strip()

    control_notes = _apply_sublet_2460_controls(
        prepared,
        po_ro_rows=list(result.po_ro_rows),
        fallback_ro=fallback_ro,
    )
    result.notes.extend(control_notes)

    for line in prepared:
        if (line.get("metaData") or {}).get("apPostingLine"):
            continue
        acct = _account_number(line)
        if acct == SUBLET_GL:
            continue  # already handled (per-line RO)
        # Non-2460 expense: Control = vendor name (both refId + refText).
        if vendor:
            set_control(line, vendor)

    # Mirror Controls onto result.postings so the dashboard GL panel matches.
    by_amt: dict[float, str] = {}
    for line in prepared:
        if _account_number(line) != SUBLET_GL:
            continue
        if (line.get("metaData") or {}).get("apPostingLine"):
            continue
        ctrl = str(line.get("refText") or "").strip()
        if ctrl:
            by_amt[round(float(line.get("amount") or 0), 2)] = ctrl
    for p in result.postings:
        if _account_number(p) != SUBLET_GL:
            continue
        if (p.get("metaData") or {}).get("apPostingLine"):
            continue
        ctrl = by_amt.get(round(float(p.get("amount") or 0), 2))
        if ctrl:
            set_control(p, ctrl)

    accounting_date_ms = result.tekion_invoice_date_ms or int(time.time() * 1000)
    inv_id = result.invoice_id or ""
    inv_no = result.invoice_number or _norm_invoice(expected.invoice_number)
    try:
        svc.post_transaction(
            inv_id,
            inv_no,
            prepared,
            accounting_date_ms=accounting_date_ms,
        )
    except Exception as exc:  # noqa: BLE001
        if "duplicate.invoice.number" in str(exc):
            raise DuplicateInvoiceNumber(
                f"Tekion refused to post: invoice number {result.invoice_number!r} is "
                "already posted for this vendor. Void the duplicate Vendor Invoice "
                "in Tekion; nothing was posted."
            ) from exc
        from api.services.closed_month import (
            first_of_current_month_ms,
            is_closed_month_error,
        )

        if not is_closed_month_error(exc):
            raise RuntimeError(
                "Verification passed and the invoice file was "
                f"{'uploaded' if result.media_id else 'not uploaded'}, but completing the "
                f"Vendor Invoice in Tekion failed: {exc}"
            ) from exc

        # Closed month (A132): retry once with 1st of the current open month.
        tz_name = "America/Chicago"
        get_tz = getattr(client, "get_dealer_timezone", None)
        if callable(get_tz):
            try:
                tz_name = str(get_tz() or tz_name)
            except Exception:  # noqa: BLE001
                pass
        bumped_ms = first_of_current_month_ms(tz_name=tz_name)
        print(
            f"[VI] Closed month on post ({exc}); bumping scheduledTime "
            f"{accounting_date_ms} -> {bumped_ms} and retrying once"
        )
        try:
            svc.post_transaction(
                inv_id,
                inv_no,
                prepared,
                accounting_date_ms=bumped_ms,
            )
        except Exception as retry_exc:  # noqa: BLE001
            raise RuntimeError(
                "Verification passed and the invoice file was "
                f"{'uploaded' if result.media_id else 'not uploaded'}, but completing the "
                f"Vendor Invoice in Tekion failed after closed-month retry: {retry_exc}"
            ) from retry_exc
        result.tekion_invoice_date_ms = bumped_ms
        result.notes.append(
            "Tekion closed month on first post; completed with accounting date "
            "set to the 1st of the current month."
        )

    result.completed = True
    result.notes.append("Completed Vendor Invoice (attach + post transaction).")
