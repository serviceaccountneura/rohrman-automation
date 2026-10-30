"""Read a vehicle manufacturer invoice's OCR into `VehicleInvoiceFacts`.

Separate from `ocr_helpers.py` because the questions are different. A parts
invoice asks "what is the total and which GL does it belong to". A vehicle
invoice asks "what is the stock number, what did the car cost the dealer, and
which of the amounts the clerk wrote in the margin is the holdback".

WHY THE SEARCH IS SO TOLERANT
    The OCR contract is not a fixed schema -- Gemini is asked to mirror the
    document's own structure, so a Kia invoice nests things differently from a
    Ford one, and the same manufacturer changes layout between months. Rather
    than chase that, every lookup here walks the whole tree looking for a
    label/value pair whose label matches, at any depth.

    The cost of tolerance is false positives, so each extractor is narrow about
    what it will accept: amounts must sit next to a label that names them,
    and the stock number must look like a stock number.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from api.services import ocr_helpers
from api.services.vmi_je_creation import VehicleInvoiceFacts, detect_manufacturer
from api.services.vmi_template import GlAnnotation

# ── Walking the OCR tree ─────────────────────────────────────────────────────


def _pairs(node: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Every (label, value) in the OCR document, at any depth.

    Two shapes count as a pair: a dict key with a scalar value, and the
    {"label": ..., "value": ...} rows the prompt produces for totals and
    identifiers. Both appear in real output, often in the same document.
    """
    if isinstance(node, dict):
        label = node.get("label") or node.get("description") or node.get("name")
        if label is not None and "value" in node:
            yield str(label), node["value"]
        if label is not None and "amount" in node:
            yield str(label), node["amount"]
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _pairs(v, str(k))
            else:
                yield str(k), v
    elif isinstance(node, list):
        for item in node:
            yield from _pairs(item, key)


def _rows(node: Any) -> Iterator[dict[str, Any]]:
    """Every dict in the OCR document, at any depth.

    `_pairs` flattens the tree and loses which row a value belonged to, which is
    fine for "find the holdback" and useless for "read across this row to its
    dealer column". Totals on a vehicle invoice are a grid, and the answer
    depends on the intersection.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _rows(value)
    elif isinstance(node, list):
        for item in node:
            yield from _rows(item)


def _normalise(text: Any) -> str:
    """Lowercase, punctuation-free, space-free — for substring matching."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _amount(value: Any) -> float | None:
    """A dollar amount, or None. Unlike `ocr_helpers._parse_amount`, this keeps
    the sign and returns None rather than 0.0 for junk — a missing holdback and
    a zero holdback lead to different decisions."""
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return round(-abs(amount) if negative else amount, 2)


def _find_amount(ocr: dict[str, Any], hints: tuple[str, ...]) -> float | None:
    """The first amount whose label contains one of `hints`."""
    for label, value in _pairs(ocr):
        flat = _normalise(label)
        if any(hint in flat for hint in hints):
            amount = _amount(value)
            if amount is not None:
                return abs(amount)
    return None


# ── The individual facts ─────────────────────────────────────────────────────

# "DEALER COST" is the column the entry is built from. MSRP is the customer
# price and posting it would overstate inventory by the markup.
_DEALER_COST_HINTS = ("dealercost", "dealerinvoice", "dealertotal", "totaldealercost")
_MSRP_HINTS = ("msrp", "retailtotal", "totalmsrp")

# Amounts a clerk writes on the invoice. The keys match what a template asks
# for in `ManufacturerTemplate.requires`.
_ANNOTATED_AMOUNT_HINTS: dict[str, tuple[str, ...]] = {
    "holdback": ("holdback", "holdbk", "hb"),
    "krs": ("krs", "retailsupport", "kiaretailsupport"),
    "doc_fee": ("docfee", "documentfee", "internaldoc", "docfeepayable"),
    "htv": ("htv", "holdbacktovehicle"),
    "marketing": ("marketing", "marketingallowance", "advertising", "adassessment"),
    "floorplan": ("floorplan", "floorplancredit", "flooring"),
}

# A stock number as the stores write it by hand: a short letter prefix then
# digits. Kia writes "SK6459"; Oakbrook Toyota writes "OBT 7992" with a space,
# and requiring the two halves to touch meant that one was read off the page
# correctly by OCR and then thrown away here.
#
# The separator is optional and dropped from the result, so both forms come out
# as a single token. Anchored at both ends so it cannot bite a fragment out of
# the VIN, which is 17 characters of exactly this alphabet.
_STOCK_PATTERN = re.compile(r"\b([A-Z]{1,3})[\s-]?(\d{3,6})\b")


def _stock_from(text: Any) -> str:
    """The stock number in some text, with any separator removed."""
    match = _STOCK_PATTERN.search(str(text or "").upper())
    return f"{match.group(1)}{match.group(2)}" if match else ""


_STOCK_LABEL_HINTS = ("stocknumber", "stockno", "stock", "stk")


# Labels that carry a dealer cost but not the FINAL one. A Kia memorandum
# invoice prints SUBTOTAL 30,638.00 above TOTAL 32,133.00, the difference being
# inland freight -- and taking the first match found the subtotal, which would
# have understated inventory by the freight on every car.
_NOT_A_FINAL_COST = ("sub", "base", "unit", "option", "freight", "handling")

# Row labels that mean "this is the bottom line". Checked before anything is
# inferred from magnitudes.
_FINAL_TOTAL_LABELS = ("totalinvoice", "invoicetotal", "totaldue", "grandtotal")

# These invoices print two money columns, and OCR may report them either as one
# row with two keys or as two rows whose labels name the column:
#
#     TOTAL INVOICE (MSRP)            40712.00
#     TOTAL INVOICE (DEALER INVOICE)  38189.40
#
# Both match "total invoice", so taking the first found read the MSRP -- the
# customer price -- as what the dealership owes. The column has to be chosen
# explicitly, not by whichever arrived first.
_MSRP_COLUMN = ("msrp", "retail", "suggested")
_DEALER_COLUMN = ("dealer", "cost", "invoiceprice")


def get_dealer_cost_total(ocr: dict[str, Any]) -> float:
    """The FINAL dealer cost -- what the dealership owes for the car.

    Never the first dealer figure on the page. These invoices print that column
    several times going down -- base, options, subtotals, total -- and taking
    whichever the OCR emitted first read Toyota's TOTAL F.I.E. of 5,094.00 as
    the price of a 38,189.40 car.
    """
    # 1. A row that names itself the final total, read across to its dealer
    #    column. This is the only reading that is certain rather than inferred,
    #    so it is tried first. Toyota prints four dealer figures down the page
    #    -- TOTAL F.I.E., TOTAL MODEL AND F.I.E., SUB TOTAL, TOTAL INVOICE --
    #    and only the last is what the dealership owes.
    fallback_total: float | None = None
    for row in _rows(ocr):
        label = _normalise(
            row.get("label") or row.get("description") or row.get("name") or ""
        )
        if not any(hint in label for hint in _FINAL_TOTAL_LABELS):
            continue

        # Never the MSRP column, whatever order it arrives in.
        if any(hint in label for hint in _MSRP_COLUMN):
            continue

        # A label that names the dealer column is the answer outright. One that
        # names no column is kept aside: it is right on an invoice with a single
        # money column, and wrong to prefer over an explicit dealer row.
        names_dealer = any(hint in label for hint in _DEALER_COLUMN)
        if not names_dealer:
            amount = _amount(row.get("value") if "value" in row else row.get("amount"))
            if amount and fallback_total is None:
                fallback_total = abs(amount)
            continue
        # A dealer-specific column if the row has one (Toyota prints MSRP and
        # DEALER INVOICE side by side)...
        for key, value in row.items():
            if any(hint in _normalise(key) for hint in _DEALER_COST_HINTS):
                amount = _amount(value)
                if amount:
                    return abs(amount)
        # ...otherwise the row's own value. Ford's totals come back as plain
        # label/value pairs -- "Invoice Total" -> "45267.70" -- with no column
        # to choose between, and requiring a dealer-named key meant skipping
        # the one row that was already the right answer.
        amount = _amount(row.get("value") if "value" in row else row.get("amount"))
        if amount:
            return abs(amount)

    if fallback_total is not None:
        return fallback_total

    # 2. Otherwise collect every dealer-column figure and take the largest.
    #    A total always exceeds its own subtotals, and taking the first match
    #    landed on whichever the OCR happened to emit first.
    candidates: list[float] = []
    for label, value in _pairs(ocr):
        flat = _normalise(label)
        if not any(hint in flat for hint in _DEALER_COST_HINTS):
            continue
        if any(bad in flat for bad in _NOT_A_FINAL_COST):
            continue
        amount = _amount(value)
        if amount is not None and amount > 0:
            candidates.append(abs(amount))
    if candidates:
        return max(candidates)

    # Fall back to the document total. On a memorandum invoice the printed
    # TOTAL is the dealer cost -- the MSRP sits in its own labelled column.
    return ocr_helpers.get_total_amount(ocr)


def get_msrp_total(ocr: dict[str, Any]) -> float:
    return _find_amount(ocr, _MSRP_HINTS) or 0.0


def get_stock_number(ocr: dict[str, Any]) -> str:
    """The stock number, which is nearly always handwritten.

    Labelled fields first, then handwriting. Bare text is searched last and only
    for the letters-then-digits shape, because an unlabelled number on a vehicle
    invoice is far more likely to be an order or key number.
    """
    for label, value in _pairs(ocr):
        if any(hint in _normalise(label) for hint in _STOCK_LABEL_HINTS):
            found = _stock_from(value)
            if found:
                return found
            text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
            if text and len(text) <= 10:
                return text

    for note in ocr.get("handwritten_notes") or []:
        found = _stock_from(note)
        if found:
            return found

    return ""


def get_annotated_amounts(ocr: dict[str, Any]) -> dict[str, float]:
    """The amounts a clerk wrote on the invoice, keyed by what they are.

    Only labelled values are taken. The handwriting on these invoices is a mix
    of GL account numbers, a stock number and dollar amounts, and guessing which
    bare number is the holdback would post an invented figure to a real
    receivable account.
    """
    found: dict[str, float] = {}
    for key, hints in _ANNOTATED_AMOUNT_HINTS.items():
        amount = _find_amount(ocr, hints)
        if amount is not None:
            found[key] = amount
    return found


# The account number inside whatever a clerk wrote. "GL 2245", "GL# 2250",
# "gl 8041" and a bare "2245" all name the same thing, and the label is written
# by hand so its shape varies by person and by day.
#
# The digits must stand alone. A stock number like OBT7992 has no word boundary
# before its digits, so it cannot be mistaken for account 7992 -- which matters,
# because a stock number sits on every one of these invoices.
_ACCOUNT_IN_TEXT = re.compile(r"\b(\d{4,5}[A-Za-z]?)\b")


# An account the writer labelled as one: "GL 2245", "GL# 2250", "ACCT 7193".
# The label is what separates the account from the amount beside it.
_MARKED_ACCOUNT = re.compile(
    r"(?:GL|G/L|ACCT|ACCOUNT|A/C)\s*#?\s*(\d{4,5}[A-Za-z]?)\b",
    re.IGNORECASE,
)


def _account_number(text: Any) -> str:
    match = _ACCOUNT_IN_TEXT.search(str(text or ""))
    return match.group(1).upper() if match else ""


def get_gl_annotations(ocr: dict[str, Any]) -> dict[str, float]:
    """Handwritten GL account -> the amount the clerk pointed it at.

    This is the heart of the vehicle flow. Staff write an account number on the
    invoice and draw an arrow to the figure that belongs in it: "2245" against
    KAC0780KAC means 780.00 goes to holdback receivable. The OCR prompt asks for
    these as a `gl_annotations` array; this reads it back.

    Amounts come back positive, and an account written more than once keeps only
    the last figure. Both are fine for DISPLAY, which is all this is for now.
    The posting logic uses get_gl_annotation_lines() instead, which keeps every
    occurrence and its sign -- see that function for why that matters.
    """
    found: dict[str, float] = {}
    # gl_mappings[] is the field the vision schema actually defines, and the
    # prompt's arrow-anchoring rules already aim at it. gl_annotations is
    # accepted as an alias so a future schema change does not silently return
    # nothing here.
    entries = list(ocr.get("gl_mappings") or []) + list(ocr.get("gl_annotations") or [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account = _account_number(entry.get("gl_account"))
        amount = _amount(entry.get("amount"))
        if not account or amount is None:
            continue
        found[account] = abs(amount)
    return found


def _gl_lines_from_notes(ocr: dict[str, Any]) -> list[GlAnnotation]:
    """Annotations read straight off the transcribed handwriting.

    The parsing lives in ocr_helpers.gl_notes because every flow needs it: the
    minus sign only survives in the raw note, and Misc, VSO and OEM lose it in
    exactly the same way the vehicle flow did.
    """
    return [
        GlAnnotation(
            account=note["account"],
            amount=note["amount"],
            label=note["label"],
            signed=note["signed"],
        )
        for note in ocr_helpers.gl_notes(ocr)
    ]


def get_gl_annotation_lines(ocr: dict[str, Any]) -> list[GlAnnotation]:
    """Every account written on the invoice, in order, with sign and label.

    A LIST, not a map. Schaumburg Honda's clerk writes 2248 three times on one
    invoice -- as DMA, as HTB and as FLOORASST -- because the store's template
    posts to 2248 three times. Keying by account keeps one of the three.

    `gl_mappings` is the primary reading, since the vision prompt's anchoring
    rules aim at it. The transcribed notes supply what that structure loses: the
    minus sign, and the label where OCR reported none. A note naming an account
    the structured output missed entirely is added rather than dropped.
    """
    notes = _gl_lines_from_notes(ocr)
    claimed: set[int] = set()
    lines: list[GlAnnotation] = []

    entries = list(ocr.get("gl_mappings") or []) + list(ocr.get("gl_annotations") or [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account = _account_number(entry.get("gl_account"))
        amount = _amount(entry.get("amount"))
        if not account or amount is None:
            continue
        label = str(entry.get("mapped_description") or entry.get("source") or "").strip()

        # The note that transcribed this same figure, for its sign and label.
        twin = next(
            (
                i
                for i, note in enumerate(notes)
                if i not in claimed
                and note.account == account
                and round(abs(note.amount), 2) == round(abs(amount), 2)
            ),
            None,
        )
        if twin is None:
            # The prompt asks for a positive figure here and direction is
            # decided downstream, so a minus arriving anyway is not noise -- it
            # means the model saw one on the page. Honour it, since without a
            # matching note this is the only place the sign could survive.
            lines.append(
                GlAnnotation(account, amount, label, signed=amount < 0)
                if amount < 0
                else GlAnnotation(account, abs(amount), label, signed=False)
            )
            continue

        claimed.add(twin)
        note = notes[twin]
        lines.append(
            GlAnnotation(
                account=account,
                amount=note.amount,
                label=label or note.label,
                signed=note.signed,
            )
        )

    # An account the notes read but the structured output did not. Added only
    # when that exact account-and-figure is not already present, so the two
    # readings of one annotation can never become two lines.
    for i, note in enumerate(notes):
        if i in claimed:
            continue
        if any(
            line.account == note.account
            and round(abs(line.amount), 2) == round(abs(note.amount), 2)
            for line in lines
        ):
            continue
        lines.append(note)

    return lines


def get_coded_amounts(ocr: dict[str, Any]) -> list[float]:
    """Amounts the manufacturer printed in cents, as dollars.

    Schaumburg Honda's invoice states its allowances in a bare run of digits on
    the MSRP line -- "42800 4400 85590" -- with no decimal point anywhere. The
    last two digits are the cents, so the rule is simply to divide by 100:
    428.00, 44.00 and 855.90.

    Two of those three are also written out by hand beside their GL accounts,
    which is what makes the convention safe to rely on: the handwriting confirms
    the reading of the row it came from.

    Anything already punctuated is left alone rather than divided -- a figure
    written "855.90" means 855.90, and treating it as cents would post 8.56.
    """
    amounts: list[float] = []
    for raw in ocr.get("coded_amounts") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        # Anything carrying a letter is a VIN, an engine number or a stock
        # number, whatever the prompt asked for. Stripping the letters off
        # 5FNRL6H68TB085198 and reading what is left turns a VIN into
        # 5,668,085,198, which is why this rejects rather than salvages.
        if any(c.isalpha() for c in text):
            continue

        if not text.isdigit():
            # Already punctuated, so it states its own decimal point: "855.90"
            # means 855.90, and dividing it by 100 would post 8.56.
            value = _amount(text)
            if value is not None and value > 0:
                amounts.append(round(abs(value), 2))
            continue
        # Under three digits cannot carry both cents and a dollar amount, and a
        # very long run is a serial number that slipped through.
        if not 3 <= len(text) <= 9:
            continue
        amounts.append(round(int(text) / 100.0, 2))
    return amounts


def get_gl_annotation_labels(ocr: dict[str, Any]) -> dict[str, str]:
    """{GL account -> the label OCR says that amount came from}.

    OCR reports its own reasoning in `mapped_description`: "PPO RESERVE",
    "WHOLESALE FINANCE RESERVE". That is the only evidence available for
    checking whether an account was tied to the right figure, and on the
    Oakbrook Toyota invoice it is what shows the two were swapped -- 2250's
    Tekion name ends in WFR, and the WFR figure had been given to 2245.
    """
    labels: dict[str, str] = {}
    for entry in list(ocr.get("gl_mappings") or []) + list(ocr.get("gl_annotations") or []):
        if not isinstance(entry, dict):
            continue
        account = _account_number(entry.get("gl_account"))
        label = str(entry.get("mapped_description") or entry.get("source") or "").strip()
        if account and label:
            labels[account] = label
    return labels


def get_annotated_gl_accounts(ocr: dict[str, Any]) -> list[str]:
    """GL account numbers written on the invoice, in reading order.

    Advisory for now: the template decides which accounts the entry uses. These
    are surfaced so a mismatch between what the clerk wrote and what the
    template chose is visible on the document detail page.
    """
    accounts: list[str] = []
    for note in ocr.get("handwritten_notes") or []:
        for match in re.finditer(r"\b(\d{4,5})\b", str(note or "")):
            account = match.group(1)
            if account not in accounts:
                accounts.append(account)
    document_level = ocr_helpers.get_document_gl_account(ocr)
    if document_level and document_level not in accounts:
        accounts.insert(0, document_level)
    return accounts


# ── Assembling the whole thing ───────────────────────────────────────────────


def build_facts(ocr: dict[str, Any], dealership_name: str = "") -> VehicleInvoiceFacts:
    """Everything the templates are allowed to draw on, from one OCR result."""
    vendor_name = ocr_helpers.get_vendor_name(ocr)
    dealership = dealership_name or ocr_helpers.get_dealership_name(ocr)

    # Ford's invoices have no invoice number: the field is "Invoice & Unit
    # Identification NO." and holds the VIN. Falling back to it keeps the
    # document row identifiable and keeps the duplicate check meaningful --
    # one VIN is one car is one invoice.
    invoice_number = ocr_helpers.get_invoice_number(ocr) or ocr_helpers.get_vin(ocr)
    annotations = get_gl_annotations(ocr)

    return VehicleInvoiceFacts(
        invoice_number=invoice_number,
        invoice_date=ocr_helpers.get_invoice_date(ocr),
        dealership_name=dealership,
        manufacturer=detect_manufacturer(vendor_name, dealership),
        vin=ocr_helpers.get_vin(ocr),
        stock_number=get_stock_number(ocr),
        dealer_cost_total=get_dealer_cost_total(ocr),
        msrp_total=get_msrp_total(ocr),
        annotated_amounts=get_annotated_amounts(ocr),
        annotated_gl_accounts=get_annotated_gl_accounts(ocr),
        gl_annotations=annotations,
        gl_annotation_lines=get_gl_annotation_lines(ocr),
        coded_amounts=get_coded_amounts(ocr),
        unpriced_gl_accounts=get_unpriced_gl_accounts(ocr, annotations),
        gl_annotation_labels=get_gl_annotation_labels(ocr),
        prose_sourced_accounts=annotations_read_from_prose(ocr),
    )


# ── Human corrections ────────────────────────────────────────────────────────

# What a person is allowed to supply after a refusal, and how it is read. Each
# key matches a field on VehicleInvoiceFacts.
#
# Deliberately a fixed list rather than "set whatever attribute is named". A
# form posts whatever the browser sends, and letting it write arbitrary
# attributes on the object that decides a journal entry is not a risk worth
# taking to save a few lines.
OVERRIDE_FIELDS: dict[str, str] = {
    # Uppercased: Tekion holds both this way, and a control typed as "sf2979"
    # would not reconcile against the stock number on the vehicle record.
    "stock_number": "upper",
    "vin": "upper",
    "invoice_number": "text",
    "invoice_date": "date",
    "dealership_name": "text",
    "manufacturer": "upper",
    "dealer_cost_total": "amount",
    "gl_annotations": "gl_map",
}


def apply_overrides(facts: VehicleInvoiceFacts, overrides: dict[str, Any]) -> list[str]:
    """Overlay a person's corrections onto what OCR read. Returns what changed.

    A person correcting a refused document outranks OCR unconditionally -- they
    are looking at the piece of paper. Blank values are ignored rather than
    treated as "clear this", so submitting a form with three of eight boxes
    filled corrects three fields instead of wiping five.

    GL annotations MERGE into what was read rather than replacing it: the usual
    correction is one account OCR missed, and replacing would silently drop the
    ones it got right.
    """
    changed: list[str] = []
    for key, kind in OVERRIDE_FIELDS.items():
        if key not in overrides:
            continue
        raw = overrides[key]

        if kind == "gl_map":
            merged = dict(facts.gl_annotations)
            for account, value in (raw or {}).items():
                account = re.sub(r"[^0-9A-Za-z]", "", str(account)).upper()
                amount = _amount(value)
                if not account or amount is None:
                    continue
                merged[account] = abs(amount)

                # The list is what actually posts, so the correction has to
                # land there too. An account already on the list is corrected
                # in place; one that is not is appended, which is the case that
                # matters -- Schaumburg Honda prints its holdback in a coded row
                # OCR cannot read, so a person types 2245 and the figure in.
                #
                # KNOWN LIMIT: a form sending {account: amount} cannot say WHICH
                # of three 2248 lines it means, so the first is corrected. A
                # store needing more than that needs a form that carries labels.
                existing = next(
                    (a for a in facts.gl_annotation_lines if a.account == account),
                    None,
                )
                if existing is None:
                    facts.gl_annotation_lines.append(
                        GlAnnotation(account=account, amount=abs(amount))
                    )
                else:
                    existing.amount = (
                        -abs(amount) if existing.amount < 0 else abs(amount)
                    )
            if merged != facts.gl_annotations:
                facts.gl_annotations = merged
                changed.append(key)
            continue

        if raw is None or str(raw).strip() == "":
            continue

        if kind == "amount":
            amount = _amount(raw)
            if amount is None:
                continue
            value: Any = abs(amount)
        elif kind == "date":
            value = ocr_helpers._normalize_date(raw)
            if not value:
                continue
        elif kind == "upper":
            value = str(raw).strip().upper()
        else:
            value = str(raw).strip()

        if getattr(facts, key) != value:
            setattr(facts, key, value)
            changed.append(key)

    if changed:
        print(f"[VMI] manual overrides applied: {', '.join(changed)}")
    return changed


# Wording that means the figure came out of a sentence rather than off a
# labelled line or a written "GL <account> <amount>" pair.
#
# Reading an arrow into a paragraph has been wrong every time it has been tried
# on a Toyota reserve line: the transcribed span comes back one line lower than
# the arrow actually points, so 2245 takes the wholesale finance figure and 2250
# takes the PPO one, and the entry balances anyway. Balancing is what makes it
# dangerous -- nothing downstream can tell.
_PROSE_SPAN_HINTS = (
    "whichincludes",
    "dealerreceives",
    "reserveof",
    "inaddition",
    "thisinvoice",
)


def annotations_read_from_prose(ocr: dict[str, Any]) -> list[str]:
    """Accounts whose amount was taken out of a sentence, not a labelled figure.

    Returns the accounts involved so the caller can refuse and say which. An
    empty list means every amount came from somewhere unambiguous.
    """
    from_prose: list[str] = []
    for account, label in get_gl_annotation_labels(ocr).items():
        flat = _normalise(label)
        if any(hint in flat for hint in _PROSE_SPAN_HINTS):
            from_prose.append(account)
    return from_prose


def get_unpriced_gl_accounts(ocr: dict[str, Any], priced: dict[str, float]) -> list[str]:
    """Accounts written on the invoice that came back WITHOUT an amount.

    OCR reports the handwriting it can see in `handwritten_notes` and the pairs
    it managed to bind in `gl_mappings`. When an account appears in the first
    and not the second, it was read off the page and then lost -- the arrow was
    not followed to a figure.

    That is not a harmless gap. On the Oakbrook Toyota invoice both 2245 and
    2250 were written; only 2245 was bound, so the entry posted three lines
    instead of seven and nobody was told. An account a person wrote and the
    system silently dropped is exactly the kind of omission that has to stop the
    document rather than shrink it.
    """
    missing: list[str] = []
    # Line by line: two accounts OCR joined into one note are still two
    # accounts, and _MARKED_ACCOUNT below takes only the first per note.
    notes = [
        line
        for raw in ocr.get("handwritten_notes") or []
        for line in ocr_helpers.note_lines(raw)
    ]
    for note in notes:
        # A note that reads "GL 2245 1129$" contains TWO four-digit numbers, and
        # only the first is an account -- the second is the amount. Reading both
        # as accounts reported 1129 as written-but-unpriced and refused an
        # invoice OCR had got completely right.
        #
        # So where a note names its account explicitly, take that and nothing
        # else from the note. Only a note with no such marker falls back to
        # scanning it for bare numbers, which is the older style where an
        # account is written on its own.
        marked = _MARKED_ACCOUNT.search(note)
        candidates = (
            [marked.group(1)]
            if marked
            else [m.group(1) for m in _ACCOUNT_IN_TEXT.finditer(note)]
        )

        for account in candidates:
            account = account.upper()
            if account in priced or account in missing:
                continue
            # A stock number is digits too -- but only worth guarding against
            # when the note did NOT label the number as an account. "GL 8041"
            # also parses as the stock pattern (letters then digits), and
            # applying the guard there discarded a genuinely missing account.
            if not marked and _stock_from(note) and account in _stock_from(note):
                continue
            missing.append(account)
    return missing
