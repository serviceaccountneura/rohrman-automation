"""AP invoice approval — ISOLATED module (not yet wired into the PO flow).

Automates the "AP Creation" SOP against Tekion's internal API, reproducing what
the web app does when a clerk approves a pre-invoiced PO:

    1. Invoice List, filtered to Status = Pre Invoice
       -> POST /api/accounting/u/poInvoice/search   (status IN [PRE_INVOICED])
    2. Open the invoice + its PO Invoice, confirm PO number / invoice number /
       dollar amount
       -> GET  /api/accounting/u/poInvoice/invoicePostings/{invoiceId}
       -> POST /api/partTrade/u/purchase/search     (universalId IN [...])
       -> POST /api/accounting/u/poInvoice/paymentInfo/byInvoiceIds
    3. "Next"
       -> POST /api/accounting/u/poInvoice/invoiceDueDate
       -> POST /api/accounting-module/u/tenant/lookup/numbers
    4. Enter invoice information on the postings:
         Description       -> posting.description  = reference number
         next to GL Account-> posting.refText      = RO number (SUBLET only)
    5. Post Transaction  -- DELIBERATELY NOT IMPLEMENTED. See post_transaction().

Endpoints were captured from a live session with `npm run pw:capture:ap` and
analysed with `npm run pw:analyze:ap` (captured/ap-endpoints.json).

INTEGRATION NOTE
    Nothing here imports the PO routes, and nothing here touches the database.
    The invoice to approve is described by an `ExpectedInvoice`, which for now
    is a hardcoded sample (HARDCODED_EXPECTED). To connect this to the PO flow
    later, build an ExpectedInvoice from the CreatePoResponse / OCR contract and
    pass it to `approve_invoice()` -- no other change is required.

SAFETY
    `approve_invoice()` runs read-only by default. Every mutating step is gated
    behind dry_run=False, and the final Post Transaction always raises, because
    its endpoint was intentionally never captured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api.services.tekion_client import TekionApiClient

# ── The record we expect to find in Tekion ────────────────────────────────────


@dataclass
class ExpectedInvoice:
    """What the invoice in Tekion *should* say.

    Later this is built from the OCR contract / the PO we just created. For now
    `HARDCODED_EXPECTED` stands in, so the matching logic can be exercised
    end-to-end without the database.
    """

    invoice_number: str
    po_number: str
    invoice_amount: float
    # Step 4: goes into the expense posting's `description` field.
    reference_number: str
    # Step 4: goes into `refText`, but only for SUBLET invoices.
    ro_number: str | None = None
    po_type: str = "MISCELLANEOUS"  # SUBLET | MISCELLANEOUS | STOCK
    # Cents tolerance when comparing dollar amounts.
    amount_tolerance: float = 0.005


# Captured from the live dry run — PO 34267 / ADVANCE AUTO PARTS at dealer 1707.
# Stands in for the DB/OCR record until this is wired into the PO flow; the
# reference number is an arbitrary placeholder, which is what step 4 writes into
# the posting Description.
#
# NOTE: this points at a real invoice sitting in the Pre Invoice queue, so the
# demo goes green. Clerks approve invoices through the day, so the record will
# eventually leave the queue and the default run will report "no matching
# invoice". That is expected -- run with --list and pick a current PO, or pass
# --po/--invoice/--amount explicitly.
HARDCODED_EXPECTED = ExpectedInvoice(
    invoice_number="4347622659413",
    po_number="34341",
    invoice_amount=85.98,
    reference_number="REF-HARDCODED-001",
    ro_number=None,
    po_type="MISCELLANEOUS",
)


@dataclass
class Discrepancy:
    field_name: str
    expected: Any
    found: Any

    def __str__(self) -> str:
        return f"{self.field_name}: expected {self.expected!r}, Tekion has {self.found!r}"


@dataclass
class ApprovalResult:
    matched: bool = False
    posted: bool = False  # always False until Post Transaction is implemented
    invoice_id: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    po_id: int | None = None
    universal_id: str | None = None
    vendor_name: str | None = None
    invoice_amount: float | None = None
    po_total: float | None = None
    due_date_ms: int | None = None
    postings: list[dict[str, Any]] = field(default_factory=list)
    prepared_postings: list[dict[str, Any]] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    # Set when the amount is wrong: the SOP says do NOT edit it, flag the Parts
    # Manager instead.
    flagged_for_parts_manager: bool = False
    notes: list[str] = field(default_factory=list)


# ── Service ───────────────────────────────────────────────────────────────────


class ApApprovalService:
    """Read/verify/prepare the AP approval. Takes an already-configured client.

    The client carries the login session and the dealer context, so this class
    can be dropped next to the existing PO code without duplicating auth.
    """

    def __init__(self, client: TekionApiClient) -> None:
        self.client = client

    # ── Step 1: the Pre Invoice list ─────────────────────────────────────────

    def search_pre_invoiced(
        self,
        search_text: str = "",
        vendor_id: str | None = None,
        rows: int = 50,
    ) -> list[dict[str, Any]]:
        """Invoice List filtered to Status = Pre Invoice.

        `search_text` matches against invoiceNumber and poDetails.poNum, which is
        how the UI's search box behaves.
        """
        filters: list[dict[str, Any]] = [
            {"field": "status", "operator": "IN", "values": ["PRE_INVOICED"], "key": "status"},
            {
                "field": "otherPayableInvoice",
                "operator": "IN",
                "values": [False],
                "key": "otherPayableInvoice",
            },
        ]
        if vendor_id:
            filters.append(
                {"field": "payeeId", "operator": "IN", "values": [str(vendor_id)], "key": "payeeId"}
            )
            filters.append(
                {"field": "payeeType", "operator": "IN", "values": ["VENDOR"], "key": "payeeType"}
            )

        res = self.client._req_json(
            "/api/accounting/u/poInvoice/search",
            method="POST",
            body={
                "sort": [{"field": "invoiceDate", "order": "DESC"}],
                "filters": filters,
                "searchText": search_text,
                "groupBy": [],
                "includeFields": [],
                "searchableFields": ["invoiceNumber", "poDetails.poNum"],
                "excludeFields": [],
                "pageInfo": {"start": 0, "rows": rows},
                "projections": [
                    {
                        "key": "dueAmount",
                        "field": "outstandingAmount.amount",
                        "metricFunction": "SUM",
                        "filters": [],
                        "includeFields": [],
                    }
                ],
            },
        )
        data = res.get("data") or {}
        hits = data.get("hits") or []
        print(f"[AP] Pre Invoice list: {len(hits)} invoice(s) (count={data.get('count')})")
        return hits

    def find_invoice(self, expected: ExpectedInvoice) -> dict[str, Any] | None:
        """Locate the pre-invoiced record for `expected`, by PO number then invoice number."""
        for term in (expected.po_number, expected.invoice_number):
            if not term:
                continue
            for hit in self.search_pre_invoiced(search_text=term):
                if self._hit_matches(hit, expected):
                    print(f"[AP] Matched invoice via '{term}'")
                    return hit
        # Fall back to an unfiltered scan — the search index can lag.
        print("[AP] Search text found nothing; scanning the full Pre Invoice list...")
        for hit in self.search_pre_invoiced():
            if self._hit_matches(hit, expected):
                return hit
        return None

    @staticmethod
    def _hit_matches(hit: dict[str, Any], expected: ExpectedInvoice) -> bool:
        if str(hit.get("invoiceNumber") or "").strip() == expected.invoice_number.strip():
            return True
        for po in hit.get("poDetails") or []:
            if str(po.get("poNum") or "").strip() == expected.po_number.strip():
                return True
        return False

    # ── Step 2: invoice detail + the PO Invoice behind it ────────────────────

    def get_invoice_postings(self, invoice_id: str) -> list[dict[str, Any]]:
        """The double-entry lines: one expense line + the AP offset (GL 3002)."""
        res = self.client._req_json(
            f"/api/accounting/u/poInvoice/invoicePostings/{invoice_id}", method="GET"
        )
        postings = res.get("data") or []
        print(f"[AP] Invoice postings: {len(postings)} line(s)")
        return postings

    def get_po(self, universal_id: str) -> dict[str, Any] | None:
        """The PO Invoice shown in the left dropdown, fetched by universalId.

        `universal_id` is the same value the PO-creation code already builds,
        e.g. "MISCELLANEOUS%10299" or "SUBLET%10299".
        """
        res = self.client._req_json(
            "/api/partTrade/u/purchase/search",
            method="POST",
            body={
                "sort": [],
                "filters": [
                    {"field": "universalId", "operator": "IN", "values": [universal_id]}
                ],
                "searchText": "",
                "groupBy": [],
                "includeFields": [],
                "searchableFields": [],
                "excludeFields": [],
                "pageInfo": {"start": 0, "rows": 9999},
            },
        )
        hits = (res.get("data") or {}).get("hits") or []
        return hits[0] if hits else None

    def get_payment_info(self, invoice_id: str) -> dict[str, Any]:
        res = self.client._req_json(
            "/api/accounting/u/poInvoice/paymentInfo/byInvoiceIds",
            method="POST",
            body={"dealerIdToInvoiceIdsMap": {self.client.current_dealer_id: [invoice_id]}},
        )
        return res.get("data") or {}

    # ── Step 3: "Next" ───────────────────────────────────────────────────────

    def get_due_date(self, invoice_date_ms: int, vendor_id: str, vendor_site_id: str) -> int | None:
        res = self.client._req_json(
            "/api/accounting/u/poInvoice/invoiceDueDate",
            method="POST",
            body={
                "invoiceDate": invoice_date_ms,
                "vendorId": vendor_id,
                "vendorSiteId": int(vendor_site_id) if str(vendor_site_id).isdigit() else vendor_site_id,
            },
        )
        data = res.get("data")
        if isinstance(data, dict):
            return data.get("invoiceDueDate")
        return data if isinstance(data, (int, float)) else None

    def lookup_vendor_numbers(self, vendor_display_id: str) -> dict[str, Any]:
        """The vendor-number lookup the Next page issues before showing the form."""
        res = self.client._req_json(
            "/api/accounting-module/u/tenant/lookup/numbers",
            method="POST",
            body={"VENDOR": {self.client.current_dealer_id: [vendor_display_id]}},
        )
        return res.get("data") or {}

    # ── Step 4: fill in the invoice information ──────────────────────────────

    @staticmethod
    def prepare_postings(
        postings: list[dict[str, Any]], expected: ExpectedInvoice
    ) -> list[dict[str, Any]]:
        """Apply the SOP's step 4 to the posting lines.

        Description       -> the reference number, on the expense line
        next to GL Account-> the RO number, on the expense line, SUBLET only

        The AP offset line (metaData.apPostingLine) is left untouched: Tekion
        owns its refText, which holds the vendor display id.
        """
        prepared: list[dict[str, Any]] = []
        for p in postings:
            line = dict(p)
            is_ap_line = bool((p.get("metaData") or {}).get("apPostingLine"))
            if not is_ap_line:
                line["description"] = expected.reference_number
                if expected.po_type == "SUBLET" and expected.ro_number:
                    line["refText"] = expected.ro_number
            prepared.append(line)
        return prepared

    # ── Step 5: Post Transaction — intentionally not implemented ─────────────

    def post_transaction(self, *_args: Any, **_kwargs: Any) -> None:
        """The final commit. NOT IMPLEMENTED ON PURPOSE.

        The dry-run capture deliberately stopped before "Post Transaction", so
        the request payload for it was never recorded. Implementing it from
        guesswork would risk posting a wrong journal entry to the GL.

        To enable it: re-run `npm run pw:capture:ap`, click through to the same
        screen, click Post Transaction once on a disposable invoice, then read
        the final write call out of captured/ap-endpoints.json and implement it
        here.
        """
        raise NotImplementedError(
            "Post Transaction was never captured (dry run stopped before it). "
            "Re-capture with npm run pw:capture:ap and click Post Transaction once "
            "to record the endpoint + payload."
        )


# ── Orchestration ─────────────────────────────────────────────────────────────


def approve_invoice(
    client: TekionApiClient,
    expected: ExpectedInvoice | None = None,
    dealership_name: str | None = None,
    dry_run: bool = True,
) -> ApprovalResult:
    """Run the AP approval SOP up to (but not including) Post Transaction.

    Args:
        client: a logged-in TekionApiClient.
        expected: the invoice we expect to find. Defaults to HARDCODED_EXPECTED.
        dealership_name: optional dealer to switch to before searching.
        dry_run: when True (default) nothing is written to Tekion.
    """
    expected = expected or HARDCODED_EXPECTED
    svc = ApApprovalService(client)
    result = ApprovalResult()

    if dealership_name:
        dealer_id = client.find_dealer_by_name(dealership_name)
        if not dealer_id:
            raise ValueError(f"Could not match dealership '{dealership_name}'")
        client.switch_dealer(dealer_id)

    print(f"[AP] Dealer {client.current_dealer_id} - looking for invoice "
          f"{expected.invoice_number!r} / PO {expected.po_number!r}")

    # ── Step 1 + 2: find it ──────────────────────────────────────────────────
    hit = svc.find_invoice(expected)
    if not hit:
        result.notes.append(
            f"No PRE_INVOICED invoice found for invoice {expected.invoice_number!r} "
            f"/ PO {expected.po_number!r}."
        )
        return result

    result.invoice_id = str(hit.get("id") or "")
    result.invoice_number = hit.get("invoiceNumber")
    result.vendor_name = hit.get("payeeName") or (hit.get("vendorDetails") or {}).get("vendorName")

    amount = hit.get("invoiceAmount")
    if isinstance(amount, dict):
        # Tekion stores money in cents.
        result.invoice_amount = (amount.get("amount") or 0) / 100.0
    elif isinstance(amount, (int, float)):
        result.invoice_amount = float(amount)

    po_details = hit.get("poDetails") or []
    if po_details:
        result.po_number = str(po_details[0].get("poNum") or "")
        result.po_id = po_details[0].get("poId")
        result.universal_id = po_details[0].get("universalId")

    # The PO Invoice from the left dropdown.
    po = svc.get_po(result.universal_id) if result.universal_id else None
    if po:
        result.po_total = po.get("totalAmount")
        result.vendor_name = (po.get("vendorDetails") or {}).get("vendorName") or result.vendor_name
        if not result.po_number:
            result.po_number = str(po.get("orderNumber") or "")

    # ── Step 2 checks: PO number, invoice number, dollar amount ──────────────
    if result.po_number and result.po_number.strip() != expected.po_number.strip():
        result.discrepancies.append(Discrepancy("po_number", expected.po_number, result.po_number))

    if (result.invoice_number or "").strip() != expected.invoice_number.strip():
        result.discrepancies.append(
            Discrepancy("invoice_number", expected.invoice_number, result.invoice_number)
        )

    if result.invoice_amount is not None and abs(
        result.invoice_amount - expected.invoice_amount
    ) > expected.amount_tolerance:
        result.discrepancies.append(
            Discrepancy("invoice_amount", expected.invoice_amount, result.invoice_amount)
        )
        # SOP: never correct the amount here.
        result.flagged_for_parts_manager = True
        result.notes.append(
            "Dollar amount does not match. Per the SOP the amount must NOT be edited - "
            "flag the Parts Manager to correct it."
        )

    result.postings = svc.get_invoice_postings(result.invoice_id)
    svc.get_payment_info(result.invoice_id)

    # ── Step 3: Next ─────────────────────────────────────────────────────────
    vendor_id = str(hit.get("payeeId") or (po or {}).get("vendorDetails", {}).get("vendorId") or "")
    vendor_site_id = str((po or {}).get("vendorDetails", {}).get("vendorSiteId") or vendor_id)
    vendor_display_id = str(
        hit.get("payeeNumber") or (po or {}).get("vendorDetails", {}).get("vendorDisplayId") or ""
    )
    invoice_date_ms = hit.get("invoiceDate") or int(
        datetime.now(tz=timezone.utc).timestamp() * 1000
    )
    if vendor_id:
        result.due_date_ms = svc.get_due_date(invoice_date_ms, vendor_id, vendor_site_id)
    if vendor_display_id:
        svc.lookup_vendor_numbers(vendor_display_id)

    # ── Step 4: fill Description / RO number ────────────────────────────────
    result.prepared_postings = svc.prepare_postings(result.postings, expected)
    result.matched = not result.discrepancies

    # ── Step 5: stop ─────────────────────────────────────────────────────────
    if dry_run:
        result.notes.append("Dry run - stopped before Post Transaction. Nothing was written.")
    else:
        svc.post_transaction()  # raises: endpoint deliberately not captured

    return result
