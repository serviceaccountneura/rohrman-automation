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
    5. Attach the invoice PDF (while still PRE_INVOICED)
       -> POST /api/accounting/u/poInvoice/preInvoice/post   (invoiceId set)
    6. Post Transaction
       -> POST /api/accounting/u/poInvoice/receivedTransaction/{invoiceId}

Endpoints were captured from a live session with `npm run pw:capture:ap` and
analysed with `npm run pw:analyze:ap` (captured/ap-endpoints.json).

INTEGRATION NOTE
    Nothing here imports the PO routes, and nothing here touches the database.
    The invoice to approve is described by an `ExpectedInvoice`, which for now
    is a hardcoded sample (HARDCODED_EXPECTED). To connect this to the PO flow
    later, build an ExpectedInvoice from the CreatePoResponse / OCR contract and
    pass it to `approve_invoice()` -- no other change is required.

SAFETY
    `approve_invoice()` runs read-only by default and never posts. The pipeline
    posts through `vi_verify._complete_vendor_invoice`, behind TEKION_PO_WRITES.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api.services.tekion_client import TekionApiClient

# ── Post Transaction (captured 29 Oct, pw:capture:ap at dealer 1707) ─────────
#
# The body is a journal entry on the AP journal, linked back to the invoice.
# The posting lines are the ones invoicePostings returns, in postingOrder, with
# only the keys below -- the GET adds lineIdentifier, tags, medias, ... which
# the UI does not send back.
_RECEIVED_TRANSACTION_PATH = "/api/accounting/u/poInvoice/receivedTransaction/{invoice_id}"
# Attaching a file to an existing Vendor Invoice re-posts the whole pre-invoice
# with its invoiceId and the media in `attachments` (captured 30 Oct). The post
# ignores assetAttachmentDto, so a file sent there never shows on the invoice.
_PRE_INVOICE_POST_PATH = "/api/accounting/u/poInvoice/preInvoice/post"
_AP_JOURNAL_NUMBER = "75"
_DOCUMENT_TYPE_SUFFIX = "document_type_5"
_WIRE_POSTING_KEYS = (
    "dealerId", "glAccountId", "amount", "refId", "refText", "refType",
    "description", "countAdjusted", "count", "postingOrder", "metaData",
    "amountCredited",
)


def set_control(line: dict[str, Any], text: str) -> None:
    """Fill a posting's Control column with free text.

    What a clerk types into Control goes out as BOTH refId and refText on a
    CUSTOM line. refText alone, with no refId, is refused at post time with
    A217 "Mandatory fields are not filled as per COA".
    """
    line["refType"] = "CUSTOM"
    line["refId"] = text
    line["refText"] = text

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
    posted: bool = False  # approve_invoice never posts; see vi_verify
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
                gid = str(p.get("glAccountId") or "")
                acct = gid.split("_")[-1] if gid else ""
                # Sublet GL 2460 → Control = RO #. All other expense GLs →
                # Control stays as clerk set (verify enforces vendor name).
                if acct == "2460" and expected.ro_number:
                    set_control(line, expected.ro_number)
                elif expected.po_type == "SUBLET" and expected.ro_number:
                    set_control(line, expected.ro_number)
            prepared.append(line)
        return prepared

    # ── Step 5: attach the invoice PDF ───────────────────────────────────────

    @staticmethod
    def build_attach_payload(invoice: dict[str, Any], media_ids: list[str]) -> dict[str, Any]:
        """Re-post body for an existing pre-invoice, as the UI sends it.

        `invoice` is the Vendor Invoice as poInvoice/search returns it. Every
        field goes back as Tekion holds it -- amounts, dates, GL lines -- so the
        only change is the attachment. A field rebuilt from anywhere else would
        overwrite the invoice.
        """
        usd0 = {"amount": 0, "currency": "USD"}
        existing = [
            str(a.get("mediaId"))
            for a in invoice.get("attachments") or []
            if a.get("mediaId")
        ]
        site = str(invoice.get("vendorSiteId") or "")
        return {
            # The UI sends 0 and an empty poDetails; Tekion keeps the PO link.
            "poAmount": usd0,
            "payeeId": str(invoice.get("payeeId") or ""),
            "vendorSiteId": int(site) if site.isdigit() else site,
            "invoiceNumber": invoice.get("invoiceNumber"),
            "invoiceAmount": invoice.get("invoiceAmount"),
            "invoiceDate": invoice.get("invoiceDate"),
            "invoiceDueDate": invoice.get("invoiceDueDate"),
            "comment": invoice.get("comment"),
            "salesTax": invoice.get("salesTax") or usd0,
            "fees": invoice.get("fees") or usd0,
            "shippingCharges": invoice.get("shippingCharges") or usd0,
            "discount": invoice.get("discount") or usd0,
            "attachments": [{"mediaId": m} for m in existing + media_ids],
            "invoiceId": invoice.get("id"),
            "type": invoice.get("type") or "INVOICE",
            "transactionDetails": {},
            "payeeName": invoice.get("payeeName"),
            "payeeNumber": invoice.get("payeeNumber"),
            "poDetails": [],
            "apGlAccountId": invoice.get("apGlAccountId"),
            "accountingDetails": invoice.get("accountingDetails") or [],
            "taxes": invoice.get("taxes") or [],
            "payeeType": invoice.get("payeeType") or "VENDOR",
        }

    def attach_to_invoice(self, invoice: dict[str, Any], media_id: str) -> dict[str, Any]:
        """Link an uploaded file to a PRE_INVOICED Vendor Invoice."""
        body = self.build_attach_payload(invoice, [media_id])
        print(f"[AP] Attach mediaId={media_id} to invoice {invoice.get('id')}")
        res = self.client._req_json(_PRE_INVOICE_POST_PATH, method="POST", body=body)
        res = res if isinstance(res, dict) else {}
        if res.get("status") == "failed":
            details = res.get("errorDetails") or {}
            raise RuntimeError(
                f"Attaching the invoice PDF was refused: {details.get('errorCode')} "
                f"{details.get('key')}: {details.get('displayMessage')}"
            )
        attached = [a.get("mediaId") for a in (res.get("data") or {}).get("attachments") or []]
        if media_id not in attached:
            raise RuntimeError(
                f"Tekion accepted the re-post but invoice {invoice.get('id')} does not "
                f"list mediaId {media_id} among its attachments."
            )
        return res

    # ── Step 6: Post Transaction ─────────────────────────────────────────────

    @staticmethod
    def build_post_payload(
        invoice_id: str,
        invoice_number: str,
        postings: list[dict[str, Any]],
        dealer_id: str,
        accounting_date_ms: int,
    ) -> dict[str, Any]:
        """The receivedTransaction body, matching the captured request.

        The PDF is not sent here: the post ignores assetAttachmentDto, so the
        file is linked beforehand by attach_to_invoice().
        """
        wire_postings = []
        for p in sorted(postings, key=lambda p: p.get("postingOrder") or 0):
            line = {k: p.get(k) for k in _WIRE_POSTING_KEYS if k in p}
            line.setdefault("dealerId", dealer_id)
            if not line.get("refType"):
                # invoicePostings returns null; the UI sends CUSTOM.
                line["refType"] = "CUSTOM"
            wire_postings.append(line)
        # The A/P line carries the invoice total as its credit.
        ap_line = next(
            (p for p in wire_postings if (p.get("metaData") or {}).get("apPostingLine")),
            None,
        )
        if ap_line is not None:
            transaction_amount = abs(float(ap_line.get("amount") or 0))
        else:
            transaction_amount = sum(
                float(p.get("amount") or 0) for p in wire_postings if float(p.get("amount") or 0) > 0
            )
        return {
            "transactionType": "GENERAL",
            "postings": wire_postings,
            "transactionAmount": round(transaction_amount, 2),
            "journalId": f"{dealer_id}_{_AP_JOURNAL_NUMBER}",
            "description": invoice_number,
            "metadata": {
                "sourceType": "RECEIVING_INVOICE",
                "businessFlow": "VENDOR_INVOICE_FLOW",
            },
            "refId__PO_INVOICE_ASSET": [invoice_id],
            "refType": "PURCHASE_ORDER_INVOICE",
            "refId": invoice_id,
            "refText": invoice_number,
            "documentTypeId": f"{dealer_id}_{_DOCUMENT_TYPE_SUFFIX}",
            "franchiseId": dealer_id,
            # The journal-entry Submit sends true; the Vendor Invoice post sends false.
            "defaultJournalValidationRequired": False,
            # Epoch-ms as a string, as the UI sends it.
            "scheduledTime": str(accounting_date_ms),
            "assetAttachmentDto": {},
            "userConsentToSkipCreditLimitCheckForAR": False,
        }

    def post_transaction(
        self,
        invoice_id: str,
        invoice_number: str,
        postings: list[dict[str, Any]],
        *,
        accounting_date_ms: int,
    ) -> dict[str, Any]:
        """Post the Vendor Invoice to the ledger. There is no undo.

        Tekion refuses an invoice number already posted for the vendor with a
        400 carrying "duplicate.invoice.number" (A111); that raises like any
        other refusal.
        """
        dealer_id = str(self.client.current_dealer_id)
        body = self.build_post_payload(
            invoice_id, invoice_number, postings, dealer_id, accounting_date_ms
        )
        path = _RECEIVED_TRANSACTION_PATH.format(invoice_id=invoice_id)
        print(f"[AP] Post Transaction: POST {path}")
        res = self.client._req_json(path, method="POST", body=body)
        if isinstance(res, dict) and res.get("status") == "failed":
            details = res.get("errorDetails") or {}
            raise RuntimeError(
                f"Post Transaction refused: {details.get('errorCode')} "
                f"{details.get('key')}: {details.get('displayMessage')}"
            )
        return res


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
        raise RuntimeError(
            "approve_invoice never posts; the pipeline posts through vi_verify."
        )

    return result
