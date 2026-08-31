"""Vehicle manufacturer invoice -> Auto Posting journal entry.

The fifth flow. A manufacturer invoice (Kia, Ford, Honda, Toyota) is downloaded
from the manufacturer's site, annotated by hand, and dropped into the Vehicle
Manufacturing folder. This turns it into a journal entry.

HOW THIS DIFFERS FROM THE OEM FLOW
    `je_creation.py` builds a *manual* entry: journal 76, two sides, the debit
    split per part. Every line is derived from the invoice total.

    A vehicle invoice is not shaped like that. One car produces seven lines
    across inventory, notes payable, holdback receivable, incentive receivable
    and internal fee accounts -- and which lines appear is a property of the
    MANUFACTURER, not of the invoice total. So the entry is driven by a
    per-manufacturer template instead:

        journal 70          VEHICLE PURCHASES
        document type 8     Vehicle Purchase Invoice
        reference type      Stock Number
        template            ai-automation-vehicle-manufacturing

WHAT COMES FROM WHERE
    Printed on the invoice   VIN, dealer cost total, MSRP, options, freight
    Written by hand          stock number, GL account numbers, and the amounts
                             that are not printed (holdback, incentives, fees)

    The handwriting is not a fallback -- it is the parts/accounting clerk
    telling us values the manufacturer does not print. Ford's holdback is the
    last 8 of the VIN against a computed amount; Honda's attachments change
    monthly. That is why the meeting settled on "the human writes it on the
    invoice and the AI reads it" rather than on parsing every manufacturer's
    layout.

WHAT IS DELIBERATELY NOT HERE
    Only the Kia template is specified, because Kia is the one manufacturer we
    have both a real invoice and the matching finished journal entry for. Ford,
    Honda and Toyota are registered but refuse: a template guessed from a
    meeting summary would post real money to real accounts.

    Refusing is the same choice `je_creation.py` makes when the parts do not sum
    to the invoice total. A wrong journal entry is worse than no journal entry,
    because nobody goes looking for one that already exists.

UNVERIFIED -- READ BEFORE TRUSTING THE WRITE PATH
    `save_draft` posts to the same endpoint the manual JE flow uses, with
    journal 70 and document type 8 substituted. That is an INFERENCE. The Auto
    Posting screen may issue a different request, and applying a named template
    almost certainly needs an endpoint we have never captured. Until someone
    captures the Auto Posting screen, run this with dry_run=True.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from api.services.je_creation import (
    Discrepancy,
    JournalEntryService,
    _parse_date,
)
from api.services.tekion_client import TekionApiClient

# ── Tekion coordinates for this flow ─────────────────────────────────────────

# The saved Auto Posting template. Named by the business, not by us.
TEMPLATE_NAME = "ai-automation-vehicle-manufacturing"

JOURNAL_NUMBER = "70"  # VEHICLE PURCHASES
DOCUMENT_TYPE_SUFFIX = "document_type_8"  # Vehicle Purchase Invoice
REFERENCE_TYPE = "STOCK_NUMBER"

_SAVE_DRAFT_METHOD = "POST"
_SAVE_DRAFT_PATH = "/api/accounting/u/v2/transaction/dealer/{dealer_id}/draft"

_REF_TEXT_MAX = 50


# ── Control numbers ──────────────────────────────────────────────────────────
#
# Each posting line carries a control value, and which one is a property of the
# line. On the Kia sample: holdback keys off the last six of the VIN, the
# incentive receivable off the full VIN, everything else off the stock number.


class Control:
    STOCK = "STOCK"
    FULL_VIN = "FULL_VIN"
    LAST_SIX_VIN = "LAST_SIX_VIN"
    LAST_EIGHT_VIN = "LAST_EIGHT_VIN"


def resolve_control(kind: str, facts: VehicleInvoiceFacts) -> str:
    if kind == Control.STOCK:
        return facts.stock_number
    if kind == Control.FULL_VIN:
        return facts.vin
    if kind == Control.LAST_SIX_VIN:
        return facts.vin[-6:] if len(facts.vin) >= 6 else ""
    if kind == Control.LAST_EIGHT_VIN:
        return facts.vin[-8:] if len(facts.vin) >= 8 else ""
    raise ValueError(f"unknown control kind {kind!r}")


# ── What we read off one invoice ─────────────────────────────────────────────


@dataclass
class VehicleInvoiceFacts:
    """Everything a template is allowed to draw on.

    Split by provenance on purpose. `printed` values can be re-read from the PDF
    at any time; `annotated` values exist only because a person wrote them down,
    so a missing one is a human step that did not happen -- a different problem
    with a different fix, and the error message should say so.
    """

    # Identity.
    invoice_number: str
    invoice_date: str  # MM/DD/YYYY
    dealership_name: str
    manufacturer: str

    vin: str = ""
    stock_number: str = ""

    # Printed on the invoice.
    dealer_cost_total: float = 0.0
    msrp_total: float = 0.0

    # Written on the invoice by hand. Keyed by the name a template asks for.
    annotated_amounts: dict[str, float] = field(default_factory=dict)
    # GL account numbers written on the invoice, in the order they were found.
    annotated_gl_accounts: list[str] = field(default_factory=list)

    def amount(self, key: str) -> float | None:
        value = self.annotated_amounts.get(key)
        return None if value is None else round(float(value), 2)


# ── Templates ────────────────────────────────────────────────────────────────


@dataclass
class TemplateLine:
    """One posting line.

    `amount` is a callable rather than a number because most lines are derived:
    the inventory debit is the dealer cost less the holdback, not a figure
    printed anywhere. Returning None means "this line does not apply to this
    invoice" and the line is dropped -- an invoice with no incentive should not
    post a zero-dollar incentive line.
    """

    gl_number: str
    control: str
    amount: Callable[[VehicleInvoiceFacts], float | None]
    description: str


@dataclass
class ManufacturerTemplate:
    name: str
    # Amount keys a human must have written on the invoice.
    requires: tuple[str, ...]
    lines: tuple[TemplateLine, ...]
    # Set when the template is registered but not yet specified.
    unspecified_reason: str = ""


def _kia_lines() -> tuple[TemplateLine, ...]:
    """The seven lines of the Kia entry, from the sample.

    Invoice 1001948819 / stock SK6459, dealer cost $32,133.00:

        2245  HOLDBACK RECEIVABLE KIA       +   780.00   last six of VIN
        3300  N/P NEW VEHICLE & DEMOS       -32,133.00   stock
        2320  NEW INV - KIA                 +31,353.00   stock
        2102  KRS RECEIVABLE                +   290.00   full VIN
        8011  KIA RETAIL SUPPORT INCOME     -   290.00   stock
        2320  NEW INV - KIA                 +   380.00   stock
        30000 Internal DOC fee payable      -   380.00   stock

    Three pairs and a split. The note payable is credited the whole dealer cost;
    that cost then lands as inventory (2320) plus holdback receivable (2245),
    which is why the inventory line is cost MINUS holdback rather than cost. The
    incentive and the DOC fee are each a debit/credit pair that nets to zero, so
    they move the money without changing the total.
    """

    def holdback(f: VehicleInvoiceFacts) -> float | None:
        return f.amount("holdback")

    def note_payable(f: VehicleInvoiceFacts) -> float | None:
        return -f.dealer_cost_total if f.dealer_cost_total else None

    def inventory(f: VehicleInvoiceFacts) -> float | None:
        hb = f.amount("holdback")
        if not f.dealer_cost_total or hb is None:
            return None
        return round(f.dealer_cost_total - hb, 2)

    def krs(f: VehicleInvoiceFacts) -> float | None:
        return f.amount("krs")

    def krs_income(f: VehicleInvoiceFacts) -> float | None:
        value = f.amount("krs")
        return None if value is None else -value

    def doc_fee(f: VehicleInvoiceFacts) -> float | None:
        return f.amount("doc_fee")

    def doc_fee_payable(f: VehicleInvoiceFacts) -> float | None:
        value = f.amount("doc_fee")
        return None if value is None else -value

    return (
        TemplateLine("2245", Control.LAST_SIX_VIN, holdback, "Holdback receivable"),
        TemplateLine("3300", Control.STOCK, note_payable, "N/P new vehicle"),
        TemplateLine("2320", Control.STOCK, inventory, "New inventory"),
        TemplateLine("2102", Control.FULL_VIN, krs, "KRS receivable"),
        TemplateLine("8011", Control.STOCK, krs_income, "Retail support income"),
        TemplateLine("2320", Control.STOCK, doc_fee, "New inventory - DOC fee"),
        TemplateLine("30000", Control.STOCK, doc_fee_payable, "Internal DOC fee payable"),
    )


_NOT_SPECIFIED = (
    "the posting template for this manufacturer has not been specified yet. "
    "Send an annotated invoice and the finished journal entry it should produce, "
    "the way the Kia sample did."
)

TEMPLATES: dict[str, ManufacturerTemplate] = {
    "KIA": ManufacturerTemplate(
        name="KIA",
        requires=("holdback", "krs", "doc_fee"),
        lines=_kia_lines(),
    ),
    # Registered so the flow reports "not specified yet" instead of "unknown
    # manufacturer" -- the first is a task, the second looks like a bug.
    "FORD": ManufacturerTemplate("FORD", (), (), unspecified_reason=_NOT_SPECIFIED),
    "HONDA": ManufacturerTemplate("HONDA", (), (), unspecified_reason=_NOT_SPECIFIED),
    "TOYOTA": ManufacturerTemplate("TOYOTA", (), (), unspecified_reason=_NOT_SPECIFIED),
}

# Matched against the vendor name OCR read off the invoice header.
_MANUFACTURER_PATTERNS = (
    ("KIA", re.compile(r"\bKIA\b", re.I)),
    ("FORD", re.compile(r"\bFORD\b", re.I)),
    ("HONDA", re.compile(r"\bHONDA\b|\bACURA\b", re.I)),
    ("TOYOTA", re.compile(r"\bTOYOTA\b|\bLEXUS\b", re.I)),
)


def detect_manufacturer(vendor_name: str, dealership_name: str = "") -> str:
    """Which manufacturer's template applies.

    The vendor name wins ("KIA AMERICA" on the header). The dealership name is
    the fallback because a Rohrman store is named for its franchise -- "Bob
    Rohrman Schaumburg Kia" -- which is right often enough to be useful and is
    never used when the invoice itself says something.
    """
    for name, pattern in _MANUFACTURER_PATTERNS:
        if pattern.search(vendor_name or ""):
            return name
    for name, pattern in _MANUFACTURER_PATTERNS:
        if pattern.search(dealership_name or ""):
            return name
    return ""


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class VehicleEntryResult:
    dealer_id: str = ""
    manufacturer: str = ""
    postings: list[dict[str, Any]] = field(default_factory=list)
    credit_total: float = 0.0
    debit_total: float = 0.0
    balance: float = 0.0
    balanced: bool = False
    problems: list[Discrepancy] = field(default_factory=list)
    refusal: str = ""
    transaction_id: str | None = None
    transaction_number: str | None = None
    journal_id: str | None = None
    saved: bool = False

    @property
    def ok(self) -> bool:
        return not self.refusal and not self.problems and self.balanced


# ── Service ──────────────────────────────────────────────────────────────────


class VehicleJournalEntryService:
    """Build and save the vehicle-purchase entry.

    Wraps `JournalEntryService` rather than subclassing it: the GL chart lookup
    and the balance check are identical and worth reusing, but the posting shape
    is not, and inheriting would invite someone to call `build_postings` and get
    a two-line parts entry with vehicle amounts in it.
    """

    def __init__(self, client: TekionApiClient) -> None:
        self.client = client
        self._je = JournalEntryService(client)

    def resolve_gl_accounts(
        self, template: ManufacturerTemplate
    ) -> tuple[dict[str, dict[str, Any]], list[Discrepancy]]:
        """Look every account the template names up in the live chart.

        Resolved per dealership, never cached across them: 2320 is "NEW INV -
        KIA" at a Kia store and something else entirely at a Ford store.
        """
        resolved: dict[str, dict[str, Any]] = {}
        problems: list[Discrepancy] = []
        for number in sorted({line.gl_number for line in template.lines}):
            acc = self._je.find_gl_account(number)
            if acc is None:
                problems.append(
                    Discrepancy(f"gl_{number}", number, "not in this dealership's chart")
                )
                continue
            if not acc.get("active", True):
                problems.append(Discrepancy(f"gl_{number}", f"{number} active", "inactive"))
            resolved[number] = acc
            print(f"[VMI] GL {number} -> {acc['account_id']}  {acc['account_name']}")
        return resolved, problems

    @staticmethod
    def build_postings(
        template: ManufacturerTemplate,
        facts: VehicleInvoiceFacts,
        resolved: dict[str, dict[str, Any]],
        dealer_id: str,
    ) -> list[dict[str, Any]]:
        """Turn template lines into the wire format.

        Amounts are DOLLARS -- the journal-entry API is the one Tekion endpoint
        that does not use cents. Sign carries the direction and `amountCredited`
        stays False on every line, matching the captured manual-JE payload.
        """
        postings: list[dict[str, Any]] = []
        order = 0
        for line in template.lines:
            value = line.amount(facts)
            if value is None or round(value, 2) == 0.0:
                continue
            acc = resolved.get(line.gl_number) or {}
            control = resolve_control(line.control, facts)
            postings.append(
                {
                    "dealerId": dealer_id,
                    "glAccountId": acc.get("account_id"),
                    "amount": round(value, 2),
                    "refId": control,
                    "refText": (line.description or control)[:_REF_TEXT_MAX],
                    "refType": REFERENCE_TYPE,
                    "countAdjusted": False,
                    "postingOrder": order,
                    "amountCredited": False,
                    # Not sent -- kept so the printed trace is readable.
                    "_glAccountNumber": line.gl_number,
                    "_glAccountName": acc.get("account_name"),
                    "_control": control,
                }
            )
            order += 1
        return postings

    @staticmethod
    def build_payload(
        facts: VehicleInvoiceFacts,
        postings: list[dict[str, Any]],
        accounting_date_ms: int,
        dealer_id: str,
        transaction_amount: float,
    ) -> dict[str, Any]:
        wire = [{k: v for k, v in p.items() if not k.startswith("_")} for p in postings]
        return {
            "transactionType": "GENERAL",
            "postings": wire,
            "transactionAmount": transaction_amount,
            "journalId": f"{dealer_id}_{JOURNAL_NUMBER}",
            "description": f"{facts.manufacturer} {facts.stock_number}".strip(),
            "metadata": {"template": TEMPLATE_NAME},
            "refId": facts.stock_number,
            "refType": REFERENCE_TYPE,
            "refText": facts.stock_number,
            "documentTypeId": f"{dealer_id}_{DOCUMENT_TYPE_SUFFIX}",
            "franchiseId": dealer_id,
            "scheduledTime": str(accounting_date_ms),
            "assetAttachmentDto": {},
        }

    def save_draft(self, payload: dict[str, Any], dealer_id: str) -> dict[str, Any]:
        """Persist as a draft.

        UNVERIFIED. This reuses the manual journal-entry draft endpoint with the
        vehicle journal and document type substituted, because the Auto Posting
        screen has never been captured. It may be the same request; it may not.
        Capture before relying on it.
        """
        path = _SAVE_DRAFT_PATH.format(dealer_id=dealer_id)
        print(f"[VMI] Save as Draft: {_SAVE_DRAFT_METHOD} {path}")
        res = self.client._req_json(path, method=_SAVE_DRAFT_METHOD, body=payload)
        data = res.get("data") or {}
        return data.get("transaction") or data


# ── Orchestration ────────────────────────────────────────────────────────────


def create_vehicle_journal_entry(
    client: TekionApiClient,
    facts: VehicleInvoiceFacts,
    *,
    dry_run: bool = True,
) -> VehicleEntryResult:
    """Build the entry, check it, and (unless dry_run) save it as a draft.

    Refuses -- rather than posting something approximate -- when the
    manufacturer has no template, when a required handwritten amount is
    missing, when the stock number or VIN needed for a control is absent, or
    when the lines do not balance to zero.
    """
    result = VehicleEntryResult(manufacturer=facts.manufacturer)

    template = TEMPLATES.get(facts.manufacturer)
    if template is None:
        result.refusal = (
            f"no posting template for manufacturer {facts.manufacturer or '(not identified)'}"
        )
        return result
    if template.unspecified_reason:
        result.refusal = f"{facts.manufacturer}: {template.unspecified_reason}"
        return result

    # Controls are not decoration -- a line with no control cannot be reconciled
    # against the vehicle later, so a missing one is a refusal, not a warning.
    if not facts.stock_number:
        result.refusal = "no stock number on the invoice (write it on before uploading)"
        return result
    needs_vin = any(line.control != Control.STOCK for line in template.lines)
    if needs_vin and len(facts.vin) < 6:
        result.refusal = "the template keys lines off the VIN, and no usable VIN was read"
        return result

    missing = [key for key in template.requires if facts.amount(key) is None]
    if missing:
        result.refusal = (
            f"{facts.manufacturer} needs {', '.join(missing)} written on the invoice; "
            "not found"
        )
        return result

    if not facts.dealer_cost_total:
        result.refusal = "no dealer cost total read from the invoice"
        return result

    dealer_id = client.current_dealer_id
    result.dealer_id = dealer_id

    service = VehicleJournalEntryService(client)
    resolved, problems = service.resolve_gl_accounts(template)
    result.problems = problems
    if problems:
        return result

    postings = service.build_postings(template, facts, resolved, dealer_id)
    result.postings = postings
    if not postings:
        result.refusal = "the template produced no posting lines for this invoice"
        return result

    credit, debit, balance = JournalEntryService.check_balance(postings)
    result.credit_total, result.debit_total, result.balance = credit, debit, balance
    result.balanced = abs(balance) < 0.005

    print(
        f"[VMI] {facts.manufacturer} {facts.stock_number}: {len(postings)} lines, "
        f"credit {credit:.2f} / debit {debit:.2f}, balance {balance:.2f}"
    )
    for p in postings:
        print(
            f"[VMI]   {p['_glAccountNumber']:>6}  {p['amount']:>12,.2f}  "
            f"{p['_control']:<20} {p['_glAccountName']}"
        )

    if not result.balanced:
        result.refusal = (
            f"the entry does not balance: credit {credit:.2f} against debit {debit:.2f} "
            f"(off by {balance:.2f})"
        )
        return result

    if dry_run:
        return result

    accounting_date_ms = int(_parse_date(facts.invoice_date).timestamp() * 1000)
    payload = service.build_payload(facts, postings, accounting_date_ms, dealer_id, debit)
    transaction = service.save_draft(payload, dealer_id)
    result.transaction_id = transaction.get("id") or transaction.get("transactionId")
    result.transaction_number = str(
        transaction.get("transactionNumber") or transaction.get("number") or ""
    ) or None
    result.journal_id = f"{dealer_id}_{JOURNAL_NUMBER}"
    result.saved = bool(result.transaction_id)
    return result
