"""Journal Entry creation — ISOLATED module (not yet wired into the PO flow).

Automates the "Parts Manufacture Ticket" SOP against Tekion's internal API,
reproducing what a clerk does on Accounting -> Journal Entry -> Create New:

    1. Create New Journal Entry -> "General" box -> "Manual"
    2. Header:
         Journal Number / Name -> always 76  (MANUFACTURERS PARTS STMT)
         Accounting Date       -> the invoice date on the Parts Ticket
         Description           -> the invoice number on the Parts Ticket
         Reference             -> the same invoice number (Reference Type = Custom)
    3. Lines -- exactly two, and they must net to $0.00:
         line 1: GL 3000 TRADE CREDITORS  CREDIT  control = MMYY of the acct date
         line 2: GL 2410 PARTS INV        DEBIT
    4. Save as Draft
         -> POST /api/accounting/u/v2/transaction/dealer/{dealerId}/draft
    5. Submit -- deliberately not implemented, see submit().

Only three values come off the Parts Ticket PDF: invoice number, invoice date,
invoice amount. Everything else is either fixed by the SOP (journal 76, GL 3000)
or derived (control number, the balancing line).

Endpoints were captured from a live session with `npm run pw:capture:je` and
analysed with `npm run pw:analyze:je` (captured/je-endpoints.json).

PAYLOAD NOTES (captured, not guessed -- these differ from the rest of the repo)
    * `amount` is in DOLLARS, not cents. Every other Tekion flow in this project
      sends cents; the journal-entry API does not.
    * The credit/debit sign lives in `amount` (negative = credit).
      `amountCredited` stays False on BOTH lines -- it is not the sign flag.
    * The line's Control column maps to the posting's `refId` + `refText` with
      `refType: "VENDOR"`. Tekion resolves it as a vendor number; an unknown
      value (like a bare MMYY) just shows a warning and still saves.
    * `journalId` is "{dealerId}_{journalNumber}", and `documentTypeId` is
      "{dealerId}_document_type_5" for "S - Payable Invoice".
    * `scheduledTime` carries the accounting date, as epoch-ms in a STRING.

INTEGRATION NOTE
    Nothing here imports the PO routes, and nothing here touches the database.
    The entry to create is described by an `ExpectedJournalEntry`, which for now
    is a hardcoded sample (HARDCODED_EXPECTED) taken from the Toyota Parts
    Ticket. To connect this to the OCR flow later, build an
    ExpectedJournalEntry from the extraction contract and pass it to
    `create_journal_entry()` -- no other change is required.

SAFETY
    `create_journal_entry()` runs read-only by default. The Save as Draft write
    is gated behind dry_run=False, and the entry is refused unless it balances
    to $0.00. Submit always raises: drafts are reversible in the UI, posted
    journal entries are not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api.services.tekion_client import TekionApiClient

# ── The captured Save as Draft call ───────────────────────────────────────────
# POST /api/accounting/u/v2/transaction/dealer/{dealerId}/draft  -> 200, DRAFT
_SAVE_DRAFT_METHOD = "POST"
_SAVE_DRAFT_PATH = "/api/accounting/u/v2/transaction/dealer/{dealer_id}/draft"

# The control number is validated against vendor numbers before the save. This
# is the same endpoint the AP approval flow uses for its vendor lookup.
_CONTROL_LOOKUP_PATH = "/api/accounting-module/u/tenant/lookup/numbers"

# "S - Payable Invoice" in the Document Type dropdown.
_DOCUMENT_TYPE_SUFFIX = "document_type_5"


# ── The entry we want to create ───────────────────────────────────────────────


@dataclass
class ExpectedJournalEntry:
    """What the journal entry should contain.

    Later this is built from the OCR contract for a Parts Ticket. For now
    `HARDCODED_EXPECTED` stands in, so the GL resolution and balancing logic can
    be exercised end-to-end without the database.
    """

    # The three values that come off the Parts Ticket PDF.
    invoice_number: str
    invoice_date: str  # MM/DD/YYYY, as printed on the ticket
    invoice_amount: float

    dealership_name: str

    # Fixed by the SOP.
    journal_number: str = "76"  # MANUFACTURERS PARTS STMT
    reference_type: str = "CUSTOM"

    # GL account numbers. 3000 is always the credit side per the SOP; the debit
    # side is hardcoded to 2410 for now and will later be classified from the
    # invoice.
    credit_gl_number: str = "3000"  # TRADE CREDITORS
    debit_gl_number: str = "2410"  # PARTS INV

    # Dollar tolerance when checking the entry balances.
    balance_tolerance: float = 0.005


# Dummy data from the sample Toyota Parts Ticket (Oakbrook Toy. in Westmont):
# invoice 6045891767, dated 05/12/2026, $147.78 -- which is exactly the entry in
# the SOP screenshot: 3000 TRADE CREDITORS -147.78 (control 0526) against
# 2410 PARTS INV - TOY EXCL TIRES 147.78, balance $0.00.
HARDCODED_EXPECTED = ExpectedJournalEntry(
    invoice_number="6045891767",
    invoice_date="05/12/2026",
    invoice_amount=147.78,
    dealership_name="Oakbrook Toyota in Westmont",
)


@dataclass
class Discrepancy:
    field_name: str
    expected: Any
    found: Any

    def __str__(self) -> str:
        return f"{self.field_name}: expected {self.expected!r}, Tekion has {self.found!r}"


@dataclass
class JournalEntryResult:
    balanced: bool = False
    saved: bool = False
    transaction_id: str | None = None
    transaction_number: str | None = None
    status: str | None = None
    journal_id: str | None = None
    description: str | None = None
    reference: str | None = None
    dealer_id: str | None = None
    accounting_date_ms: int | None = None
    control_number: str | None = None
    # False when the control number does not resolve to a vendor. Tekion only
    # warns about this (the red triangle in the UI), so it is not fatal.
    control_number_known: bool | None = None
    credit_total: float = 0.0
    debit_total: float = 0.0
    balance: float = 0.0
    # The two GL accounts as resolved against Tekion's chart of accounts.
    resolved_accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    postings: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_date(date_str: str) -> datetime:
    """Parse the Parts Ticket date. MM/DD/YYYY is what the ticket prints."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised invoice date {date_str!r} (expected MM/DD/YYYY)")


def _control_number(accounting_date: datetime) -> str:
    """The 3000 line's control number: the month and year, as MMYY.

    The SOP says the 3000 account "is controlled by the current month and year".
    In the sample, accounting date 05/12/2026 -> control "0526".
    """
    return accounting_date.strftime("%m%y")


# ── Service ───────────────────────────────────────────────────────────────────


class JournalEntryService:
    """Resolve/build/validate/save the journal entry. Takes a configured client.

    The client carries the login session and the dealer context, so this class
    can be dropped next to the existing PO code without duplicating auth.
    """

    def __init__(self, client: TekionApiClient) -> None:
        self.client = client
        self._gl_cache: list[dict[str, Any]] | None = None

    # ── Step 0: the chart of accounts ────────────────────────────────────────

    def gl_accounts(self, refresh: bool = False) -> list[dict[str, Any]]:
        """The dealership's GL accounts, straight from Tekion.

        Cached for the life of this service instance. Deliberately not persisted
        -- this module stays database-free, unlike gl_service_misc.py which
        caches the same fetch in `tekion_gl_accounts` for the PO flow.
        """
        if self._gl_cache is None or refresh:
            self._gl_cache = self.client.fetch_gl_accounts()
        return self._gl_cache

    def find_gl_account(self, account_number: str) -> dict[str, Any] | None:
        """Match a hardcoded SOP account number to the live account.

        Returns the account dict (account_id, account_number, account_name, ...)
        or None when the dealership's chart has no such account.
        """
        wanted = str(account_number).strip()
        for acc in self.gl_accounts():
            if str(acc.get("account_number", "")).strip() == wanted:
                return acc
        return None

    def resolve_accounts(
        self, expected: ExpectedJournalEntry
    ) -> tuple[dict[str, dict[str, Any]], list[Discrepancy]]:
        """Resolve both SOP account numbers against the live chart of accounts."""
        resolved: dict[str, dict[str, Any]] = {}
        problems: list[Discrepancy] = []

        for role, number in (
            ("credit", expected.credit_gl_number),
            ("debit", expected.debit_gl_number),
        ):
            acc = self.find_gl_account(number)
            if acc is None:
                problems.append(
                    Discrepancy(f"{role}_gl_account", number, "not in this dealership's chart")
                )
                continue
            if not acc.get("active", True):
                problems.append(Discrepancy(f"{role}_gl_account", f"{number} active", "inactive"))
            resolved[role] = acc
            print(f"[JE] {role:6s} GL {number} -> {acc['account_id']}  {acc['account_name']}")

        return resolved, problems

    # ── Control number check (advisory) ──────────────────────────────────────

    def control_number_exists(self, control_number: str) -> bool:
        """Does the control number resolve to a vendor?

        The UI issues this before saving and shows a warning triangle when the
        count is 0 -- which is what a bare MMYY control does. The draft saves
        either way, so callers should treat a False as a note, not a failure.
        """
        res = self.client._req_json(
            _CONTROL_LOOKUP_PATH,
            method="POST",
            body={"VENDOR": {self.client.current_dealer_id: [control_number]}},
        )
        data = (res.get("data") or {}).get("VENDOR") or {}
        entry = data.get(self.client.current_dealer_id) or {}
        return bool(entry.get("count"))

    # ── Steps 2-3: build the postings ────────────────────────────────────────

    @staticmethod
    def build_postings(
        expected: ExpectedJournalEntry,
        resolved: dict[str, dict[str, Any]],
        control_number: str,
        dealer_id: str,
    ) -> list[dict[str, Any]]:
        """The two balanced postings, in the captured wire format.

        Line 1 is always GL 3000 as the CREDIT, carrying the MMYY control number
        in refId/refText with refType VENDOR. Line 2 is the parts inventory
        account as the balancing DEBIT.

        Amounts are DOLLARS (the JE API does not use cents) and the sign carries
        the credit/debit direction -- `amountCredited` stays False on both lines,
        which is what the captured payload does.
        """
        amount = round(expected.invoice_amount, 2)
        credit = resolved.get("credit") or {}
        debit = resolved.get("debit") or {}

        return [
            {
                "dealerId": dealer_id,
                "glAccountId": credit.get("account_id"),
                "amount": -amount,  # negative == credit
                "refId": control_number,
                "refText": control_number,
                "refType": "VENDOR",
                "countAdjusted": False,
                "postingOrder": 0,
                "amountCredited": False,
                # Not sent to Tekion — kept for printing/debugging.
                "_glAccountNumber": credit.get("account_number", expected.credit_gl_number),
                "_glAccountName": credit.get("account_name"),
            },
            {
                "dealerId": dealer_id,
                "glAccountId": debit.get("account_id"),
                "amount": amount,
                "refType": "CUSTOM",
                "countAdjusted": False,
                "postingOrder": 1,
                "amountCredited": False,
                "_glAccountNumber": debit.get("account_number", expected.debit_gl_number),
                "_glAccountName": debit.get("account_name"),
            },
        ]

    @staticmethod
    def check_balance(postings: list[dict[str, Any]]) -> tuple[float, float, float]:
        """Total the credits and debits. The SOP requires the balance to be $0.00.

        Returns (credit_total, debit_total, balance) in dollars.
        """
        credit_total = round(sum(-p["amount"] for p in postings if p["amount"] < 0), 2)
        debit_total = round(sum(p["amount"] for p in postings if p["amount"] > 0), 2)
        return credit_total, debit_total, round(debit_total - credit_total, 2)

    @staticmethod
    def build_payload(
        expected: ExpectedJournalEntry,
        postings: list[dict[str, Any]],
        accounting_date_ms: int,
        dealer_id: str,
        transaction_amount: float,
    ) -> dict[str, Any]:
        """Assemble the Save as Draft body, matching the captured request.

        Description, Reference (refId) and refText all carry the invoice number,
        per the SOP.
        """
        wire_postings = [
            {k: v for k, v in p.items() if not k.startswith("_")} for p in postings
        ]
        return {
            "transactionType": "GENERAL",
            "postings": wire_postings,
            "transactionAmount": transaction_amount,
            "journalId": f"{dealer_id}_{expected.journal_number}",
            "description": expected.invoice_number,
            "metadata": {},
            "refId": expected.invoice_number,
            "refType": expected.reference_type,
            "refText": expected.invoice_number,
            "documentTypeId": f"{dealer_id}_{_DOCUMENT_TYPE_SUFFIX}",
            "franchiseId": dealer_id,
            # Epoch-ms as a string is what the UI sends.
            "scheduledTime": str(accounting_date_ms),
            "assetAttachmentDto": {},
        }

    # ── Step 4: Save as Draft ────────────────────────────────────────────────

    def save_draft(self, payload: dict[str, Any], dealer_id: str) -> dict[str, Any]:
        """Persist the entry as a draft. Returns the created transaction."""
        path = _SAVE_DRAFT_PATH.format(dealer_id=dealer_id)
        print(f"[JE] Save as Draft: {_SAVE_DRAFT_METHOD} {path}")
        res = self.client._req_json(path, method=_SAVE_DRAFT_METHOD, body=payload)
        data = res.get("data") or {}
        return data.get("transaction") or data

    # ── Step 5: Submit — intentionally not implemented ───────────────────────

    def submit(self, *_args: Any, **_kwargs: Any) -> None:
        """The final post. NOT IMPLEMENTED ON PURPOSE.

        Draft first, by explicit instruction: a draft is reversible in the UI, a
        posted journal entry is not. Capture the Submit click the same way Save
        as Draft was captured before implementing this.
        """
        raise NotImplementedError(
            "Submit is intentionally not implemented — the flow stops at Save as Draft. "
            "Re-capture with npm run pw:capture:je and click Submit once to record it."
        )


# ── Orchestration ─────────────────────────────────────────────────────────────


def create_journal_entry(
    client: TekionApiClient,
    expected: ExpectedJournalEntry | None = None,
    dealership_name: str | None = None,
    dry_run: bool = True,
) -> JournalEntryResult:
    """Run the Parts Manufacture Ticket SOP up to (but not including) Submit.

    Args:
        client: a logged-in TekionApiClient.
        expected: the entry to create. Defaults to HARDCODED_EXPECTED.
        dealership_name: dealer to switch to. Defaults to expected.dealership_name.
        dry_run: when True (default) nothing is written to Tekion.
    """
    expected = expected or HARDCODED_EXPECTED
    svc = JournalEntryService(client)
    result = JournalEntryResult()

    # ── Dealer context ───────────────────────────────────────────────────────
    target_dealership = dealership_name or expected.dealership_name
    if target_dealership:
        dealer_id = client.find_dealer_by_name(target_dealership)
        if not dealer_id:
            raise ValueError(f"Could not match dealership '{target_dealership}'")
        client.switch_dealer(dealer_id)
    result.dealer_id = client.current_dealer_id

    print(f"[JE] Dealer {result.dealer_id} - journal {expected.journal_number}, "
          f"invoice {expected.invoice_number!r}, ${expected.invoice_amount:.2f}")

    # ── Step 2: header ───────────────────────────────────────────────────────
    accounting_date = _parse_date(expected.invoice_date)
    result.accounting_date_ms = int(accounting_date.timestamp() * 1000)
    result.control_number = _control_number(accounting_date)
    result.journal_id = f"{result.dealer_id}_{expected.journal_number}"
    # Both fields track the invoice number, per the SOP.
    result.description = expected.invoice_number
    result.reference = expected.invoice_number

    print(f"[JE] Accounting date: {accounting_date.strftime('%m/%d/%Y')} "
          f"-> control {result.control_number}")

    # ── Step 0/3: resolve GL accounts against the live chart ─────────────────
    resolved, problems = svc.resolve_accounts(expected)
    result.resolved_accounts = resolved
    result.discrepancies.extend(problems)

    if len(resolved) < 2:
        result.notes.append(
            "Could not resolve both GL accounts for this dealership — nothing was built."
        )
        return result

    # Advisory: the UI warns when the control number is not a known vendor.
    result.control_number_known = svc.control_number_exists(result.control_number)
    if not result.control_number_known:
        result.notes.append(
            f"Control number {result.control_number!r} does not match a vendor — Tekion "
            "shows a warning on this field but still allows the draft to save."
        )

    # ── Step 3: the two postings, and the balance check ──────────────────────
    result.postings = svc.build_postings(
        expected, resolved, result.control_number, result.dealer_id
    )
    result.credit_total, result.debit_total, result.balance = svc.check_balance(result.postings)
    result.balanced = abs(result.balance) <= expected.balance_tolerance

    print(f"[JE] Credit ${result.credit_total:.2f}  Debit ${result.debit_total:.2f}  "
          f"Balance ${result.balance:.2f}")

    if not result.balanced:
        result.discrepancies.append(Discrepancy("balance", 0.0, result.balance))
        result.notes.append(
            "Entry does not balance to $0.00. Per the SOP it must NOT be saved — "
            "the debit and credit sides have to match first."
        )
        return result

    result.payload = svc.build_payload(
        expected,
        result.postings,
        result.accounting_date_ms,
        result.dealer_id,
        result.debit_total,
    )

    # ── Step 4: Save as Draft ────────────────────────────────────────────────
    if dry_run:
        result.notes.append("Dry run - stopped before Save as Draft. Nothing was written.")
    else:
        txn = svc.save_draft(result.payload, result.dealer_id)
        result.saved = True
        result.transaction_id = str(txn.get("id") or "")
        result.transaction_number = str(txn.get("transactionNumber") or "")
        result.status = txn.get("status")
        print(f"[JE] Draft saved: transaction {result.transaction_number} "
              f"(id={result.transaction_id}, status={result.status})")

    return result
