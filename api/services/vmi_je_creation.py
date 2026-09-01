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
        template            whichever auto-posting template at THAT dealership
                            posts to journal 70 -- found by journal, not by
                            name, because every store names its own

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

THE ACCOUNTS ARE NOT OURS TO CHOOSE
    An earlier version of this module carried a hardcoded set of GL accounts per
    manufacturer. That was backwards, and it is gone.

    Each dealership's own journal-70 auto-posting template already lists the
    accounts that store posts to. The clerk annotating the invoice writes those
    same account numbers on the page, with an arrow to the amount each one
    takes: "2245" against KAC0780KAC means 780.00 belongs in holdback
    receivable. So building the entry is a JOIN between the template and the
    handwriting -- see api/services/vmi_template.py -- and the same code serves
    Kia, Ford, Honda and Toyota without a per-make table.

    What still refuses: an annotation naming an account the template has no line
    for, and an entry that does not balance. Both mean money would land
    somewhere nobody asked for, and a wrong journal entry is worse than none,
    because nobody goes looking for one that already exists.

THE WRITE PATH, FROM THE CAPTURE
    Templates   POST /api/accounting/u/v2/transaction/upc/templates
                {"templateTypes": ["DEFAULT"]}
    Save draft  POST /api/accounting/u/v2/transaction/dealer/{id}/draft

    Applying a template turned out to be client-side pre-fill only: the saved
    transaction comes back with `templateId: null`. So the template decides
    which lines exist, and the save is the same plain draft call the manual
    journal entry already uses. There is no third call to make.
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
from api.services import vmi_template
from api.services.tekion_client import TekionApiClient

# ── Tekion coordinates for this flow ─────────────────────────────────────────

# The auto-posting template is found by its JOURNAL, never by its name. Every
# dealership maintains its own templates with its own naming, so there is no one
# name to look for -- but exactly one of them posts to journal 70, and that is
# the one this flow wants. Matching on a name would work at one store and
# silently pick the wrong template, or none, at the other eighteen.
JOURNAL_NUMBER = "70"  # VEHICLE PURCHASES
DOCUMENT_TYPE_SUFFIX = "document_type_8"  # Vehicle Purchase Invoice

# From the capture, not a guess. The transaction's own refType is CUSTOM; the
# per-line refType comes from the template and is VEHICLE on the vehicle
# accounts, CUSTOM elsewhere. An earlier version sent "STOCK_NUMBER" for both,
# which Tekion does not use anywhere in this flow.
TRANSACTION_REF_TYPE = "CUSTOM"
LINE_REF_TYPE_VEHICLE = "VEHICLE"
LINE_REF_TYPE_CUSTOM = "CUSTOM"

_SAVE_DRAFT_METHOD = "POST"
_SAVE_DRAFT_PATH = "/api/accounting/u/v2/transaction/dealer/{dealer_id}/draft"

# The dealership's auto-posting templates. Body {"templateTypes": ["DEFAULT"]}.
_TEMPLATES_PATH = "/api/accounting/u/v2/transaction/upc/templates"

# Some dealerships keep more than one template on journal 70 -- 1707 has both
# "NEW VEHICLE" and "2024 HONDA PROLOGUE". Journal number alone does not
# identify one there, so name the intended template per dealer here. Without an
# entry the flow refuses and lists the candidates rather than picking one.
# Empty, and it should stay that way unless a store genuinely keeps two
# journal-70 templates. An entry was briefly added here for 1707/"NEW VEHICLE"
# on the belief that it was Schaumburg Kia's; it was Schaumburg Honda's, seen
# only because the dealer switch above was missing. Schaumburg Kia has exactly
# one journal-70 template, "BILL".
TEMPLATE_PREFERENCE: dict[str, str] = {}

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
    # The real input: {GL account -> the amount the clerk pointed it at}.
    # "2245" against KAC0780KAC means 780.00 belongs in holdback receivable.
    gl_annotations: dict[str, float] = field(default_factory=dict)

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
    # VEHICLE on the vehicle accounts (inventory, floor plan), CUSTOM on the
    # rest. Tekion's own templates set this per line, so we do too.
    ref_type: str = LINE_REF_TYPE_CUSTOM


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

    V, C = LINE_REF_TYPE_VEHICLE, LINE_REF_TYPE_CUSTOM
    return (
        TemplateLine("2245", Control.LAST_SIX_VIN, holdback, "Holdback receivable", C),
        TemplateLine("3300", Control.STOCK, note_payable, "N/P new vehicle", V),
        TemplateLine("2320", Control.STOCK, inventory, "New inventory", V),
        TemplateLine("2102", Control.FULL_VIN, krs, "KRS receivable", C),
        TemplateLine("8011", Control.STOCK, krs_income, "Retail support income", C),
        TemplateLine("2320", Control.STOCK, doc_fee, "New inventory - DOC fee", V),
        TemplateLine("30000", Control.STOCK, doc_fee_payable, "Internal DOC fee payable", C),
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
    # Which of the dealership's auto-posting templates matched journal 70.
    # Recorded for the audit trail: the name differs at every store.
    tekion_template_name: str = ""
    # {GL account -> amount} as read off the invoice's handwriting.
    gl_annotations: dict[str, float] = field(default_factory=dict)
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
                    # CONTROLS ARE THE ONE UNVERIFIED PART. The captured draft
                    # was a minimal test with no control values filled, so it
                    # shows no refId -- while the finished Kia entry clearly
                    # carries one per line (043152, SK6459, the full VIN).
                    # refId is what the manual journal entry uses and is
                    # accepted there, so it is what we send. The template
                    # response also carries `controlNumberList` and
                    # `control2Type`, which may be the real mechanism here.
                    # Re-capture with the controls filled in to settle it.
                    "refId": control,
                    # Tekion's own payload puts the line label in `description`
                    # and leaves refText off the posting entirely.
                    "description": (line.description or None),
                    "refType": line.ref_type,
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

    # Tekion's own Control 2 vocabulary, from the journal entry screen. The
    # captured template left control2Type null, so this only takes effect on
    # templates where a store has set it; everything else keys off the stock
    # number, which is what the majority of lines use.
    _CONTROL2 = {
        "LASTSIXOFVIN": Control.LAST_SIX_VIN,
        "LASTSIXVIN": Control.LAST_SIX_VIN,
        "LASTEIGHTOFVIN": Control.LAST_EIGHT_VIN,
        "LASTEIGHTVIN": Control.LAST_EIGHT_VIN,
        "FULLVIN": Control.FULL_VIN,
        "VIN": Control.FULL_VIN,
        "STK": Control.STOCK,
        "STKNUMBER": Control.STOCK,
        "STOCKNUMBER": Control.STOCK,
    }

    @classmethod
    def _control_for(cls, line: Any, facts: VehicleInvoiceFacts) -> str:
        """The control value for one filled line.

        Tekion carries this per line as `control2Type` -- the Control 2 column
        reads "LAST SIX OF VIN", "FULL VIN" or "STK #" on the journal entry
        screen. Where a store has set it we follow it. Where it is null we use
        the stock number, which is what most lines use and what the transaction
        itself is referenced by.
        """
        raw = re.sub(r"[^A-Z]", "", str(getattr(line, "control2_type", "") or "").upper())
        kind = cls._CONTROL2.get(raw, Control.STOCK)
        control = resolve_control(kind, facts)
        # A VIN-keyed line on an invoice with no readable VIN falls back rather
        # than posting an empty control, which reconciles to nothing.
        return control or facts.stock_number

    @classmethod
    def build_postings_from_template(
        cls,
        filled: "vmi_template.FillResult",
        facts: VehicleInvoiceFacts,
        dealer_id: str,
    ) -> list[dict[str, Any]]:
        """Turn filled template lines into the captured wire format.

        Amounts are DOLLARS -- the journal-entry API is the one Tekion endpoint
        that does not use cents. Sign carries the direction and `amountCredited`
        stays False on every line, matching the captured payload.
        """
        postings: list[dict[str, Any]] = []
        for order, line in enumerate(filled.lines):
            control = cls._control_for(line, facts)
            postings.append(
                {
                    "dealerId": dealer_id,
                    # Straight from the template: no chart lookup needed, and no
                    # chance of resolving to a different account than the store
                    # configured.
                    "glAccountId": line.gl_account_id,
                    "amount": line.amount,
                    "refId": control,
                    "description": line.description or None,
                    "refType": line.ref_type,
                    "countAdjusted": False,
                    "postingOrder": order,
                    "amountCredited": False,
                    # Not sent -- kept so the printed trace is readable.
                    "_glAccountNumber": line.gl_number,
                    "_control": control,
                    "_source": line.source,
                }
            )
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
            # Filled from the resolved template once the Auto Posting calls
            # are captured; the journal id below already selects journal 70.
            "metadata": {},
            "refId": facts.stock_number,
            "refType": TRANSACTION_REF_TYPE,
            "refText": facts.stock_number,
            "documentTypeId": f"{dealer_id}_{DOCUMENT_TYPE_SUFFIX}",
            "franchiseId": dealer_id,
            "scheduledTime": str(accounting_date_ms),
            # Captured as {"attachments": []}, not {}.
            "assetAttachmentDto": {"attachments": []},
        }

    def fetch_templates(self) -> list[dict[str, Any]]:
        """Every auto-posting template configured at the current dealership."""
        res = self.client._req_json(
            _TEMPLATES_PATH, method="POST", body={"templateTypes": ["DEFAULT"]}
        )
        return (res.get("data") or {}).get("templateList") or []

    def find_template(self, dealer_id: str) -> tuple[dict[str, Any] | None, str]:
        """The dealership's auto-posting template for journal 70.

        Returns (template, problem). Selected by journal, never by name: each
        store names its own -- 1714 calls it "VEHICLE INVOICES", 1707 calls it
        "NEW VEHICLE" -- so matching on a name works at one store and quietly
        fails at the rest.

        Journal number is not always unique either. 1707 keeps two templates on
        journal 70, "NEW VEHICLE" and "2024 HONDA PROLOGUE", and taking the
        first would post a Kia against a Honda-specific template. When more than
        one matches this refuses and names them, unless TEMPLATE_PREFERENCE says
        which one that dealer means.
        """
        wanted = f"{dealer_id}_{JOURNAL_NUMBER}"
        matches = [t for t in self.fetch_templates() if t.get("journalId") == wanted]

        if not matches:
            return None, (
                f"dealer {dealer_id} has no auto-posting template on journal "
                f"{JOURNAL_NUMBER} (Vehicle Purchases)"
            )
        if len(matches) == 1:
            return matches[0], ""

        preferred = TEMPLATE_PREFERENCE.get(dealer_id)
        if preferred:
            for t in matches:
                if str(t.get("templateName") or "").strip() == preferred:
                    return t, ""
            return None, (
                f"TEMPLATE_PREFERENCE names {preferred!r} for dealer {dealer_id}, "
                f"but no template on journal {JOURNAL_NUMBER} has that name"
            )

        names = ", ".join(repr(t.get("templateName")) for t in matches)
        return None, (
            f"dealer {dealer_id} has {len(matches)} templates on journal "
            f"{JOURNAL_NUMBER} ({names}); add the intended one to "
            "TEMPLATE_PREFERENCE in vmi_je_creation.py"
        )

    def save_draft(self, payload: dict[str, Any], dealer_id: str) -> dict[str, Any]:
        """Persist as a draft.

        Captured, not inferred: the Auto Posting screen posts to the same
        endpoint the manual journal entry uses, with journal 70 and document
        type 8. Applying a template is purely client-side pre-fill -- the saved
        transaction comes back with `templateId: null` -- so the template is how
        the lines are CHOSEN, never part of how they are SAVED.
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
    """Build the entry from the dealership's own template, and save it as a draft.

    Refuses -- rather than posting something approximate -- when the store has
    no journal-70 template or more than one, when nothing was annotated on the
    invoice, when an annotation names an account the template has no line for,
    or when the finished lines do not balance to zero.
    """
    result = VehicleEntryResult(manufacturer=facts.manufacturer)
    result.gl_annotations = dict(facts.gl_annotations)

    if not facts.stock_number:
        result.refusal = "no stock number on the invoice (write it on before uploading)"
        return result
    if not facts.gl_annotations:
        result.refusal = (
            "no GL accounts were read off the invoice. The clerk writes an account "
            "number beside each amount it takes (2245 -> 780.00); without those "
            "there is nothing to post"
        )
        return result

    # ── Dealer context ───────────────────────────────────────────────────────
    # Belt and braces: the pipeline already wraps this call in `dealer_scope`,
    # which switches inside the Tekion lock. This repeat covers the other
    # callers -- the dry-run script, a future route -- because the cost of
    # getting it wrong is posting one store's money into another's books.
    #
    # THIS IS LOAD-BEARING. The Tekion client is a singleton whose dealership is
    # mutable state, so current_dealer_id is whatever the LAST job left it at.
    # Omitting this switch made a Schaumburg Kia invoice read Schaumburg Honda's
    # templates, and had the accounts happened to match it would have posted a
    # Kia purchase into Honda's books. Every flow that touches Tekion switches
    # first; this one has to as well.
    if facts.dealership_name:
        dealer_id = client.find_dealer_by_name(facts.dealership_name)
        if not dealer_id:
            result.refusal = (
                f"could not match dealership {facts.dealership_name!r} in Tekion"
            )
            return result
        client.switch_dealer(dealer_id)

    dealer_id = client.current_dealer_id
    result.dealer_id = dealer_id
    print(f"[VMI] dealer {dealer_id} ({facts.dealership_name or 'from client'})")
    service = VehicleJournalEntryService(client)

    tekion_template, template_problem = service.find_template(dealer_id)
    if template_problem:
        result.refusal = template_problem
        return result
    result.tekion_template_name = str(tekion_template.get("templateName") or "")
    print(
        f"[VMI] template {result.tekion_template_name!r} "
        f"(journal {tekion_template.get('journalId')}), "
        f"annotations {facts.gl_annotations}"
    )
    # The template's own lines. Printed because the captured template list was
    # truncated by the capture's body cap, so this is the only reliable view of
    # what a store actually has configured.
    for tp in tekion_template.get("postings") or []:
        print(
            f"[VMI]   template line {str(tp.get('glAccountId')):<14} "
            f"preset={float(tp.get('amount') or 0):>11,.2f}  "
            f"{tp.get('refType')}  {tp.get('description')}"
        )

    filled = vmi_template.fill(
        tekion_template, facts.gl_annotations, facts.dealer_cost_total
    )

    # An annotation with nowhere to go is the clearest possible signal that this
    # invoice and this template disagree. Posting the rest would quietly drop
    # money a person explicitly placed.
    if filled.unmatched_annotations:
        pairs = ", ".join(
            f"{gl}={amount:,.2f}" for gl, amount in filled.unmatched_annotations.items()
        )
        result.refusal = (
            f"the invoice annotates {pairs}, but template "
            f"{result.tekion_template_name!r} has no line for "
            f"{'those accounts' if len(filled.unmatched_annotations) > 1 else 'that account'}"
        )
        return result

    if not filled.lines:
        result.refusal = "the template produced no posting lines for this invoice"
        return result

    postings = service.build_postings_from_template(filled, facts, dealer_id)
    result.postings = postings

    credit, debit, balance = JournalEntryService.check_balance(postings)
    result.credit_total, result.debit_total, result.balance = credit, debit, balance
    result.balanced = abs(balance) < 0.005

    print(
        f"[VMI] {facts.manufacturer or 'vehicle'} {facts.stock_number}: "
        f"{len(postings)} lines, credit {credit:,.2f} / debit {debit:,.2f}, "
        f"balance {balance:.2f}"
    )
    for p in postings:
        print(
            f"[VMI]   {p['_glAccountNumber']:>6}  {p['amount']:>13,.2f}  "
            f"{p['_control']:<20} {p['_source']}"
        )

    if not result.balanced:
        result.refusal = (
            f"the entry does not balance: credit {credit:,.2f} against debit "
            f"{debit:,.2f} (off by {balance:.2f})"
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
