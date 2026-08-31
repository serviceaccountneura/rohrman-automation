"""Vendor Stock Order pre-invoicing — ISOLATED module.

Automates what a clerk does on Parts -> Purchase Order for a vendor stock order
whose goods have arrived and whose invoice has turned up:

    1. Purchase Order list, search the PO number printed on the invoice
         -> POST /api/partTrade/u/purchase/search   (orderNumber IN [...])
    2. Open that PO -> Invoices tab -> "Create Pre Invoice"
         -> /parts/purchase-order/parts/{poId}/preinvoice
    3. Attach the invoice PDF
         -> POST /api/media-v3/u/v2/initiate-upload, then PUT to S3
    4. Fill invoice number / date / amount and Submit
         -> the pre-invoice chain (getInvoiceDate, invoiceDueDate, postings, post)

UNLIKE THE OTHER FLOWS, THIS ONE CREATES NOTHING
    Sublet, misc and stock all *create* a purchase order. Here the PO already
    exists — the parts were ordered and received long before the invoice landed.
    The job is to find it and attach the bill. That means a wrong PO number is a
    hard stop, not something to work around: there is nothing to fall back to.

VERIFIED AGAINST A REAL CAPTURE
    `npm run pw:capture:vso` recorded a clerk pre-invoicing PO 34441. The parts
    screen turned out to drive the same accounting endpoints as sublet and misc,
    so the existing pre-invoice chain is reused as-is.

    One thing the capture corrected: `preInvoicing/postings` hands back the
    expense breakdown, one posting per part line, and that is what the browser
    sends as `accountingDetails` — not a single line from one GL account. The
    postings rebuilt from that capture match the browser's payload exactly.

    Still unverified: the document upload. The captured run attached no file, so
    `attachments` was empty. The upload itself is the same `upload_document()`
    the misc flow already uses successfully, but it has not been exercised on
    this screen.

SAFETY
    `pre_invoice_stock_order()` runs read-only by default. Everything through
    "check the amounts line up" is local; the write is gated behind
    dry_run=False.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.services.tekion_client import TekionApiClient

# Tekion's AP liability account, the same one sublet and misc post against.
_AP_GL_SUFFIX = "3002"


@dataclass
class ExpectedStockInvoice:
    """The invoice to attach to an existing purchase order.

    Built from the OCR contract by the pipeline. The PO number is the one field
    with no fallback — without it there is nothing to attach to.
    """

    po_number: str
    invoice_number: str
    invoice_amount: float
    dealership_name: str
    sales_tax: float = 0.0
    invoice_date: str | None = None  # MM/DD/YYYY as printed
    # Local path to the invoice file, attached to the pre-invoice when present.
    invoice_file_path: str | None = None
    # Original filename, shown in Tekion. The path above is usually a temp file.
    invoice_file_name: str | None = None
    # The GL account written on the invoice, usually by hand in the margin.
    # Consulted only when Tekion has no account of its own for these parts.
    gl_account: str = ""
    # Dollar tolerance when comparing the invoice against the PO.
    amount_tolerance: float = 0.005


@dataclass
class Discrepancy:
    field_name: str
    expected: Any
    found: Any

    def __str__(self) -> str:
        return f"{self.field_name}: invoice says {self.expected!r}, PO says {self.found!r}"


@dataclass
class StockInvoiceResult:
    matched: bool = False
    posted: bool = False
    po_id: int | None = None
    po_number: str | None = None
    universal_id: str | None = None
    po_type: str | None = None
    po_total: float | None = None
    po_status: str | None = None
    vendor_name: str | None = None
    dealer_id: str | None = None
    invoice_id: str | None = None
    media_id: str | None = None
    # Amount actually posted against the PO — the invoice net of tax.
    net_amount: float | None = None
    discrepancies: list[Discrepancy] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class VsoPreInvoiceService:
    """Find the PO, attach the invoice, pre-invoice it.

    Takes an already-configured client, so this drops in next to the existing
    flows without duplicating auth or dealer handling.
    """

    def __init__(self, client: TekionApiClient) -> None:
        self.client = client

    # ── Step 1: find the purchase order ──────────────────────────────────────

    def find_po(self, po_number: str) -> dict[str, Any] | None:
        """The PO the invoice refers to, by its printed number."""
        po = self.client.find_purchase_order(po_number)
        if po:
            print(f"[VSO] PO {po_number} -> id={po.get('id')} "
                  f"status={po.get('status')} total={po.get('totalAmount')}")
        return po

    # ── Step 2: attach the invoice ───────────────────────────────────────────

    def upload_invoice(self, file_path: str, display_name: str | None = None) -> str | None:
        """Push the invoice PDF to Tekion's media service. Returns the mediaId.

        Best-effort: a pre-invoice without its attachment is still correct
        accounting, and failing the whole run over a missing PDF would be worse
        than posting it and attaching the file by hand.
        """
        try:
            media_id = self.client.upload_document(file_path, display_name=display_name)
            print(f"[VSO] invoice attached, mediaId={media_id}")
            return media_id
        except Exception as e:  # noqa: BLE001
            print(f"[VSO] could not attach the invoice ({e}); continuing without it")
            return None

    # ── Step 3: the amounts have to line up ──────────────────────────────────

    @staticmethod
    def check_amounts(
        po: dict[str, Any], expected: ExpectedStockInvoice
    ) -> tuple[float, list[Discrepancy]]:
        """Compare the invoice against the PO it is being attached to.

        Tekion refuses to invoice a PO for a different amount than it is worth —
        the same "unable to update PO" that bites the misc flow. Catching it here
        turns an opaque Tekion 500 into something a clerk can act on.

        Returns the net amount to post (invoice less tax) and any discrepancies.
        """
        net = round(expected.invoice_amount - expected.sales_tax, 2)
        po_total = po.get("totalAmount")
        problems: list[Discrepancy] = []

        if isinstance(po_total, (int, float)):
            if abs(float(po_total) - net) > expected.amount_tolerance:
                problems.append(Discrepancy("amount", net, float(po_total)))
        return net, problems

    # ── Step 4: post it ──────────────────────────────────────────────────────

    def pre_invoice(
        self,
        po: dict[str, Any],
        expected: ExpectedStockInvoice,
        dealer_id: str,
        net_amount: float,
        media_id: str | None,
    ) -> dict[str, str]:
        """Run the pre-invoice chain against the existing PO."""
        vendor = po.get("vendorDetails") or {}
        vendor_id = str(vendor.get("vendorId") or vendor.get("id") or "")
        if not vendor_id:
            raise ValueError(f"PO {po.get('orderNumber')} has no vendor on it")

        # Tekion normally answers with a GL account per part line, taken from
            # its own parts inventory setup, and that is authoritative -- it is
            # how the parts department has actually configured these items.
            #
            # When it answers with nothing, there is still an invoice with an
            # account written on it. Using that beats posting to an empty GL,
            # which is what an unset gl_account_id would do.
            fallback_gl = (
                f"{dealer_id}_{expected.gl_account}" if expected.gl_account else ""
            )
            if fallback_gl:
                print(
                    f"[VSO] invoice names GL {expected.gl_account}; will use it only "
                    "if Tekion returns no postings of its own"
                )

            universal_id = po.get("universalId") or f"PARTS%{po.get('id')}"
        # The universalId carries the PO type Tekion expects back, e.g.
        # "MISCELLANEOUS%10366" -> "MISCELLANEOUS".
        po_type = str(universal_id).split("%")[0] or "PARTS"

        return self.client.pre_invoice(
            vendor_id=vendor_id,
            vendor_site_id=str(vendor.get("vendorSiteId") or vendor_id),
            vendor_name=vendor.get("vendorName") or "",
            vendor_display_id=str(vendor.get("vendorDisplayId") or ""),
            dealer_id=dealer_id,
            po_id=po["id"],
            po_number=str(po.get("orderNumber") or expected.po_number),
            universal_id=universal_id,
            invoice_number=expected.invoice_number,
            invoice_amount=expected.invoice_amount,
            # A stock order carries its own GL accounts on the parts lines, and
            # Tekion returns one posting per part. This is only consulted when
            # that call comes back empty.
            gl_account_id=fallback_gl,
            ap_gl_account_id=f"{dealer_id}_{_AP_GL_SUFFIX}",
            ref_text=expected.po_number,
            po_type=po_type,
            sales_tax=expected.sales_tax,
            invoice_date=expected.invoice_date,
            attachment_media_ids=[media_id] if media_id else None,
            use_returned_postings=True,
        )


# ── Orchestration ─────────────────────────────────────────────────────────────


def pre_invoice_stock_order(
    client: TekionApiClient,
    expected: ExpectedStockInvoice,
    dealership_name: str | None = None,
    dry_run: bool = True,
) -> StockInvoiceResult:
    """Attach an invoice to an existing vendor stock order and pre-invoice it.

    Args:
        client: a logged-in TekionApiClient.
        expected: the invoice, including the PO number it refers to.
        dealership_name: dealer to switch to. Defaults to expected.dealership_name.
        dry_run: when True (default) nothing is written to Tekion.
    """
    svc = VsoPreInvoiceService(client)
    result = StockInvoiceResult()

    # ── Dealer context ───────────────────────────────────────────────────────
    target = dealership_name or expected.dealership_name
    if target:
        dealer_id = client.find_dealer_by_name(target)
        if not dealer_id:
            raise ValueError(f"Could not match dealership '{target}'")
        client.switch_dealer(dealer_id)
    result.dealer_id = client.current_dealer_id

    print(f"[VSO] Dealer {result.dealer_id} - PO {expected.po_number!r}, "
          f"invoice {expected.invoice_number!r}, ${expected.invoice_amount:.2f}")

    # ── Step 1: the PO must exist ────────────────────────────────────────────
    if not expected.po_number:
        result.notes.append(
            "No purchase order number was read from the invoice. A vendor stock "
            "order is invoiced against an existing PO, so there is nothing to "
            "attach this to."
        )
        return result

    po = svc.find_po(expected.po_number)
    if not po:
        result.notes.append(
            f"No purchase order {expected.po_number!r} at this dealership. Check "
            f"the number on the invoice, and that the PO belongs to this store."
        )
        return result

    result.po_id = po.get("id")
    result.po_number = str(po.get("orderNumber") or "")
    result.universal_id = po.get("universalId")
    result.po_type = str(result.universal_id or "").split("%")[0] or None
    result.po_status = po.get("status")
    result.vendor_name = (po.get("vendorDetails") or {}).get("vendorName")
    po_total = po.get("totalAmount")
    result.po_total = float(po_total) if isinstance(po_total, (int, float)) else None

    # A cancelled or closed PO cannot take an invoice. The lookup deliberately
    # searches every status so this can be said plainly, rather than returning
    # "no such PO" for one that plainly exists.
    if str(po.get("status") or "").upper() in ("CANCELLED", "CANCELED", "VOIDED", "CLOSED"):
        result.discrepancies.append(Discrepancy("status", "an open PO", po.get("status")))
        result.notes.append(
            f"PO {result.po_number} is {po.get('status')} and cannot be invoiced."
        )
        return result

    # Already invoiced? Posting again would double-bill the vendor.
    if str(po.get("invoiceStatus") or "").upper() not in ("", "NOT_PRE_INVOICED"):
        result.discrepancies.append(
            Discrepancy("invoiceStatus", "NOT_PRE_INVOICED", po.get("invoiceStatus"))
        )
        result.notes.append(
            f"PO {result.po_number} is already {po.get('invoiceStatus')}. Posting "
            "again would invoice it twice."
        )
        return result

    # ── Step 2: amounts ──────────────────────────────────────────────────────
    net, problems = svc.check_amounts(po, expected)
    result.net_amount = net
    result.discrepancies.extend(problems)

    if problems:
        result.notes.append(
            f"The invoice is ${net:.2f} net of tax but PO {result.po_number} is "
            f"${result.po_total}. Tekion will not attach an invoice to a PO worth "
            "a different amount — check the invoice against the order."
        )
        return result

    result.matched = True

    if dry_run:
        result.notes.append("Dry run - stopped before posting. Nothing was written.")
        return result

    # ── Step 3 + 4: attach and post ──────────────────────────────────────────
    if expected.invoice_file_path:
        result.media_id = svc.upload_invoice(
            expected.invoice_file_path, expected.invoice_file_name
        )

    posted = svc.pre_invoice(po, expected, result.dealer_id, net, result.media_id)
    result.invoice_id = posted.get("invoiceId")
    result.posted = True
    print(f"[VSO] Pre-invoiced PO {result.po_number}: invoiceId={result.invoice_id}")

    return result
