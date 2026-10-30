"""Fill a dealership's Tekion auto-posting template from an annotated invoice.

THE MODEL, CORRECTED
    An earlier version of this flow carried a hardcoded set of GL accounts per
    manufacturer. That was backwards. The accounts are not ours to decide: each
    dealership's own journal-70 auto-posting template already lists them, and
    the clerk annotating the invoice writes those same account numbers on the
    page next to the amount each one takes.

    So the job is a JOIN, not a lookup table:

        Tekion template  ->  which GL accounts this store posts to, in order
        invoice writing  ->  how much goes in some of them
        the invoice      ->  the printed figures the rest are derived from

    That is why the same code works for Kia, Ford, Honda and Toyota without a
    per-make table: what differs between manufacturers is which amounts get
    written on the page, and the template already says where they land.

WHERE EACH LINE'S AMOUNT COMES FROM, IN ORDER
    1. An annotation naming that GL account. The clerk wrote "2245" with an
       arrow to 780.00, so 2245 takes 780.00. This wins over everything --
       it is a person stating the answer.
    2. A ROLE the template describes. "Vehicle floor plan amount" is the whole
       dealer cost as a credit; "Vehicle Invoice Price" is dealer cost minus
       holdback. These are computed, never written down, and the template's own
       line descriptions are what identify them.
    3. The template's preset amount. Some pairs are fixed at the store -- the
       DOC fee sits in the template as 380.00 / -380.00 -- and simply carry.
    4. Nothing. The line is dropped rather than posted at zero.

MIRROR LINES
    Templates pair a receivable with its income/payable account: DMA 150.00
    against DMA_ -150.00. The trailing underscore is the convention. When the
    first of a pair is filled from an annotation, its mirror follows with the
    sign flipped, because nobody writes the same number twice on an invoice.

WHY AN ACCOUNT NUMBER IS NOT ENOUGH TO IDENTIFY A LINE
    This module used to take {account -> amount}, which assumes each account
    appears once. Schaumburg Honda disproves it. Its template posts to 2248
    three times -- as DMA, as HTB, and as FLOORASST -- and to 2320 twice, once
    for the vehicle and once for the DOC fee. The clerk writes 2248 three times
    too, with a different figure each time.

    A dictionary keeps the last of those and drops the rest, so an invoice
    carrying 150 / 641.93 / 428 against 2248 posted 428 three times and came out
    39,052.78 short.

    The template already says which is which: its `description` field holds the
    very words the clerk writes beside the number -- "DMA", "HTB", "FLOORASST",
    "FUELALLOWANCE" -- and OCR returns that word in `mapped_description`. So an
    annotation is (account, amount, label) and the join is on the PAIR, with the
    account alone as the fallback for the stores that annotate without labels.

SIGNS
    The magnitude is the clerk's; the direction is usually the template's. But
    where a template line has no preset to take a sign from and plays no role,
    the minus sign on the page is the only evidence there is -- "3010A -641.93"
    is a credit and posting it as a debit is how the HTB and FLOORASST pairs
    both came out doubled instead of cancelling.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

# ── Roles a template line can play ───────────────────────────────────────────
#
# Matched against the template line's own `description`, which is set per
# dealership in Tekion. Substring matching on a normalised description, because
# stores write "Vehicle Holdback amount" and "VEHICLE HOLDBACK" alike.

ROLE_HOLDBACK = "holdback"
ROLE_FLOOR_PLAN = "floor_plan"
ROLE_INVOICE_PRICE = "invoice_price"

# Matched against the template line's description AND the GL account's own name
# from the chart. The names are what carry the meaning in practice:
# "HOLDBACK RECEIVABLE-KIA", "N/P NEW VEHICLE & DEMOS", "NEW INV - KIA".
# The internal DOC fee is a pair: the inventory account is debited and the DOC
# fee payable credited for the same figure. It is not on the invoice and not
# preset in every store's template, so it arrives as a per-dealership constant
# and is anchored on the payable account, which is the half that names itself.
_DOC_FEE_PAYABLE_HINTS = ("docfeepayable", "internaldocfee", "docfee")

# Every store names these accounts differently, and the names are all we have.
# Kia: "N/P NEW VEHICLE & DEMOS" and "NEW INV - KIA".
# Ford: "NOTES PAY - NEW VEHICLES" and "INV-NEW CAR".
# The first set matched and the second did not, so Ford's floor plan posted as
# a debit and its inventory line mirrored the account above it. Both spellings
# of each idea are listed rather than trying to be clever about word order.
_ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ROLE_HOLDBACK, ("holdback", "holdbk")),
    (
        ROLE_FLOOR_PLAN,
        ("floorplan", "notepay", "notespay", "notepayable", "npnew"),
    ),
    (
        ROLE_INVOICE_PRICE,
        ("invoiceprice", "newinv", "invnew", "inventory"),
    ),
)


def _normalise(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# THE ACCOUNT NUMBER DECIDES THE ROLE. Rohrman runs one chart of accounts
# across its stores, so the same number means the same thing everywhere:
#
#     2245   holdback receivable        Kia and Ford both
#     3300   notes payable / floor plan Kia and Ford both
#     2320   new vehicle inventory      Kia and Ford both
#
# Only the NAMES differ -- Kia writes "N/P NEW VEHICLE & DEMOS" where Ford
# writes "NOTES PAY - NEW VEHICLES" -- and matching on those meant Ford's floor
# plan went unrecognised and posted as a debit. The number was sitting right
# there the whole time.
_ROLE_BY_ACCOUNT = {
    "2245": ROLE_HOLDBACK,
    "3300": ROLE_FLOOR_PLAN,
    "2320": ROLE_INVOICE_PRICE,
}


def role_of(*texts: Any, gl_number: str = "") -> str:
    """Which role a line plays.

    The account number first, since it is the same at every store. Names are
    kept as a fallback for an account this map does not know, and because a
    store could one day use a different number for the same idea -- but they are
    no longer what the common cases depend on.
    """
    role = _ROLE_BY_ACCOUNT.get(str(gl_number or "").strip().upper())
    if role:
        return role
    # An account the map does not know gets a role from its name only as a last
    # resort. Names are shared across accounts that do different jobs: "FLOOR
    # PLAN ASST." is an allowance, not the floor plan itself, and matching it
    # to the floor-plan role posted the price of the car twice.
    if gl_number and str(gl_number).strip():
        return _role_from_names(*texts)

    return _role_from_names(*texts)


def _role_from_names(*texts: Any) -> str:
    """A role guessed from an account name. Weaker than the number, and known
    to be fallible -- see the caller."""
    for text in texts:
        flat = _normalise(text)
        if not flat:
            continue
        for role, hints in _ROLE_HINTS:
            if any(hint in flat for hint in hints):
                return role
    return ""


def gl_number_of(gl_account_id: Any, chart: dict[str, dict[str, Any]] | None = None) -> str:
    """The ACCOUNT NUMBER for a template line's glAccountId.

    Read this before changing it: glAccountId is NOT the account number, even
    though it is built to look exactly like one. Schaumburg Kia's holdback line
    comes back as "1710_2246" and the account it posts to is 2245 -- the id and
    the number are simply different values that happen to share a shape.

    Splitting the id was how this module started, and it silently mismatched
    every annotation against an account one digit away from the right one. So
    the chart of accounts is the only real answer; the split remains as a
    fallback for callers with no chart, and is wrong often enough that it should
    never be relied on for a posting decision.
    """
    key = str(gl_account_id or "")
    if chart:
        entry = chart.get(key)
        if entry:
            return str(entry.get("account_number") or "")
    return key.split("_", 1)[1] if "_" in key else key


# ── What the clerk wrote ─────────────────────────────────────────


@dataclass
class GlAnnotation:
    """One account written on the invoice, with the figure beside it.

    A LIST of these, not a dict: the same account is written more than once on
    invoices whose template posts to it more than once. See the module header.
    """

    account: str
    # Signed exactly as written. -641.93 means a minus sign was on the page.
    amount: float
    # The word written beside it: "HTB", "FLOORASST", "DMA". This is what tells
    # three 2248 annotations apart, and it matches the template line's own
    # description because both come from the same store vocabulary.
    label: str = ""
    # Whether that minus sign was actually read, as opposed to the amount simply
    # arriving positive. Only an explicit minus is allowed to set a direction.
    signed: bool = False


# ── Result of filling one template ───────────────────────────────────────────


@dataclass
class FilledLine:
    gl_number: str
    gl_account_id: str
    amount: float
    ref_type: str
    description: str
    # How this line got its amount, for the printed trace and the audit note.
    source: str
    # Tekion's Control 2 vocabulary for this line ("LAST SIX OF VIN", "STK #"),
    # carried through so the caller can pick the right control value.
    control2_type: str = ""


@dataclass
class FillResult:
    lines: list[FilledLine] = field(default_factory=list)
    # Annotations the template has no line for. Not fatal on its own, but it
    # means the entry will not carry money the person intended to place, so
    # callers should refuse rather than post a short one.
    #
    # A list, not a dict, for the same reason annotations are: two leftover
    # annotations on the same account are two separate problems.
    unmatched_annotations: list[GlAnnotation] = field(default_factory=list)
    # Roles the template asks for that nothing could fill. A dropped holdback
    # line also silently drops the invoice-price line computed from it, so the
    # caller needs to name the real cause rather than report an imbalance.
    dropped_roles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def fill(
    template: dict[str, Any],
    annotations: list[GlAnnotation],
    dealer_cost_total: float,
    chart: dict[str, dict[str, Any]] | None = None,
    doc_fee: float | None = None,
    holdback_fallback: float | None = None,
) -> FillResult:
    """Fill one Tekion template from one invoice's annotations.

    `template` is a raw template object from
    POST /api/accounting/u/v2/transaction/upc/templates.
    `annotations` is what the clerk wrote, in reading order. `chart` maps
    glAccountId to the account's own record, and is what turns a template line's
    opaque id into the number the clerk wrote. `doc_fee` is the store's internal
    DOC fee, which appears on no invoice.
    """
    result = FillResult()
    postings = template.get("postings") or []
    roles_filled: set[str] = set()

    by_description: dict[str, list[dict[str, Any]]] = {}
    for p in postings:
        by_description.setdefault(str(p.get("description") or ""), []).append(p)

    # WHICH ANNOTATION GOES ON WHICH LINE, decided up front.
    #
    # Deciding it inside the line loop is what the account-keyed dict did, and
    # it cannot work: the loop reaches 2320 "Vehicle Invoice Price" before it
    # reaches the 2320 DOC-fee line, so the DOC-fee annotation landed on the
    # invoice-price line and destroyed both.
    assigned, leftover = _assign(postings, annotations, chart)
    result.unmatched_annotations = leftover

    # The figure the holdback line takes, and the one the invoice price is
    # computed from. Both are read off whatever was assigned to the line that
    # plays that role, so a store that annotates them and a store that does not
    # go down the same path.
    holdback = _assigned_to_role(postings, assigned, ROLE_HOLDBACK, chart)

    # An invoice that prints its holdback instead of annotating it. Used ONLY
    # where the handwriting named no holdback account, so it can never displace
    # what a person wrote; the caller decides whether such a figure exists and
    # is unambiguous.
    holdback_from_page = False
    if holdback is None and holdback_fallback is not None:
        holdback = holdback_fallback
        holdback_from_page = True

    # What the vehicle was financed for. The clerk's annotation on the floor
    # plan account wins over anything parsed off the page.
    #
    # This is what makes the entry balance by construction. Floor plan is
    # credited X, inventory debited X minus holdback, holdback debited holdback
    # -- three lines that net to zero for ANY X. Taking X from the annotation
    # for one line and from a parsed total for another is how Ford came out
    # 1,727.30 apart: the annotation said 45,267.70 and the totals block was
    # read as 46,995.00, the MSRP on the same row.
    financed = _assigned_to_role(postings, assigned, ROLE_FLOOR_PLAN, chart)
    if financed is None:
        financed = dealer_cost_total
    financed = abs(financed) if financed else 0.0

    # The DOC fee pair, resolved by index before the main pass. The payable
    # account names itself ("Internal DOC fee payable"); the line immediately
    # above it is the inventory side that carries the matching debit.
    doc_fee_by_index: dict[int, float] = {}
    if doc_fee:
        for i, p in enumerate(postings):
            name = _account_name(p.get("glAccountId"), chart) or str(p.get("description") or "")
            if any(hint in _normalise(name) for hint in _DOC_FEE_PAYABLE_HINTS):
                doc_fee_by_index[i] = -abs(doc_fee)
                if i > 0:
                    doc_fee_by_index[i - 1] = abs(doc_fee)
                break

    dropped: list[str] = []

    for index, p in enumerate(postings):
        gl = gl_number_of(p.get("glAccountId"), chart)
        description = str(p.get("description") or "")
        account_name = _account_name(p.get("glAccountId"), chart)
        preset = p.get("amount")
        preset = None if preset is None else round(float(preset), 2)
        role = role_of(description, account_name, gl_number=gl)

        amount: float | None = None
        source = ""
        annotation = assigned.get(index)

        # 1. Written on the invoice against this line.
        if annotation is not None:
            amount = _annotated_amount(annotation, role, preset)
            source = f"annotated {annotation.account}"
            if annotation.label:
                source += f" ({annotation.label})"
            # An annotated line still CONSUMES its role. Without this, 3300
            # taking the floor plan from the handwriting left the floor-plan
            # role unclaimed, and Oakbrook Toyota's "8041 FLOOR PLAN ASST." --
            # an allowance account, not the note payable -- matched it by name
            # and credited the entire invoice a second time.
            if role:
                roles_filled.add(role)

        # 2. A role the template describes, computed from the invoice. Ahead of
        # the mirror rule because an inventory line that happens to sit under an
        # annotated one is still inventory: at Schaumburg Kia, NEW INV follows
        # N/P NEW VEHICLE, and mirroring it posted the full dealer cost instead
        # of cost less holdback.
        #
        # Only the FIRST line playing a role takes it. Both stores list their
        # inventory account twice -- once for the vehicle, once for the DOC fee
        # -- and filling both from this rule posted the price of the car twice.
        elif role and role not in roles_filled and financed:
            if role == ROLE_FLOOR_PLAN:
                amount = -financed
                source = "amount financed (credit)"
                roles_filled.add(role)
            elif role == ROLE_INVOICE_PRICE and holdback is not None:
                amount = round(financed - abs(holdback), 2)
                source = "financed less holdback"
                roles_filled.add(role)
            elif role == ROLE_HOLDBACK and holdback_from_page:
                # Reached only when no annotation named this account: an
                # annotated holdback line is filled by rule 1 above.
                amount = abs(holdback)
                source = "holdback printed on the invoice"
                roles_filled.add(role)

        # 3. A figure the store configured into the template. Schaumburg Ford
        # keeps its DOC fee here as 380.00 / -380.00; Schaumburg Kia leaves the
        # same lines at zero and the figure comes from STORE_DOC_FEE instead.
        #
        # Ahead of both mirror rules: a number a person typed into the template
        # is a decision, and inferring over the top of it is how Ford's DOC fee
        # line became a -904.00 mirror of the holdback above it.
        elif preset:
            amount = preset
            source = "template preset"

        # 4. The store's DOC fee, for templates that leave the line at zero.
        elif index in doc_fee_by_index:
            amount = doc_fee_by_index[index]
            source = "store DOC fee"

        # 5. The line immediately after an annotated one mirrors it. Templates
        # pair a receivable with the income account that offsets it -- KRS
        # RECEIVABLE then KIA RETAIL SUPPORT INCOME -- and only the receivable
        # is ever written on the invoice, because writing the same number twice
        # is what the pairing exists to avoid.
        #
        # Restricted to following an ANNOTATED line on purpose. Allowing a
        # mirror to follow a mirror would walk the rest of the template filling
        # in alternating signs: at Schaumburg Kia that would have put +290 into
        # CUSTOMER WE OWE, which has nothing to do with this invoice.
        elif (index - 1) in assigned:
            amount = -_partner_amount(postings[index - 1], assigned[index - 1], chart)
            source = f"mirrors {assigned[index - 1].account}"

        # 6. The older named-pair convention: "DMA_" follows "DMA".
        elif description.endswith("_") and description[:-1] in by_description:
            partner = by_description[description[:-1]][0]
            partner_index = postings.index(partner)
            if partner_index in assigned:
                # The mirror always opposes its partner, which is the whole
                # point of the pair: DMA 150.00 against DMA_ -150.00.
                amount = -_partner_amount(partner, assigned[partner_index], chart)
                source = f"mirrors {assigned[partner_index].account}"

        if amount is None or round(amount, 2) == 0.0:
            # A role line nothing could fill is worth naming. A missing holdback
            # also silently drops the invoice-price line computed from it, and
            # the caller reporting "the entry does not balance" sends whoever
            # reads it looking two lines further down than the actual cause.
            if role and role not in roles_filled and role not in dropped:
                dropped.append(role)
            continue

        result.lines.append(
            FilledLine(
                gl_number=gl,
                gl_account_id=str(p.get("glAccountId") or ""),
                amount=round(amount, 2),
                ref_type=str(p.get("refType") or "CUSTOM"),
                description=description or account_name,
                source=source,
                control2_type=str(p.get("control2Type") or ""),
            )
        )

    result.dropped_roles = dropped
    return result


# ── Matching annotations to template lines ─────────────────────────────


def _assign(
    postings: list[dict[str, Any]],
    annotations: list[GlAnnotation],
    chart: dict[str, dict[str, Any]] | None,
) -> tuple[dict[int, GlAnnotation], list[GlAnnotation]]:
    """Decide which template line each annotation fills.

    Two passes, and the order matters. Every annotation that carries a LABEL
    claims its line first, so an unlabelled one cannot take a line a labelled
    one was going to need.

    Returns (template index -> annotation, annotations with nowhere to go).
    """
    numbers = [gl_number_of(p.get("glAccountId"), chart) for p in postings]
    assigned: dict[int, GlAnnotation] = {}

    # Pass 1: account AND label. The template's description is the store's own
    # word for the line -- "HTB", "FLOORASST" -- and the clerk writes that same
    # word on the page, which is the only thing that tells three 2248s apart.
    #
    # Every pair is SCORED and the strongest match is taken first, rather than
    # walking the annotations in order and giving each the first line it likes.
    # Order-of-appearance is not evidence: two annotations on one account can
    # both half-match the same line, and whichever came first won.
    scored: list[tuple[float, int, int]] = []
    for a_index, annotation in enumerate(annotations):
        for line_index, p in enumerate(postings):
            if numbers[line_index] != annotation.account:
                continue
            score = _label_score(annotation.label, p.get("description"))
            if score >= _LABEL_THRESHOLD:
                scored.append((score, a_index, line_index))

    # Best score first; ties fall back to reading order so this stays stable.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    matched: set[int] = set()
    for _score, a_index, line_index in scored:
        if a_index in matched or line_index in assigned:
            continue
        assigned[line_index] = annotations[a_index]
        matched.add(a_index)

    unlabelled = [a for i, a in enumerate(annotations) if i not in matched]

    # Pass 2: account number alone, which is how every store that annotates
    # without labels has always worked.
    leftover: list[GlAnnotation] = []
    for annotation in unlabelled:
        candidates = [
            i
            for i in range(len(postings))
            if i not in assigned and numbers[i] == annotation.account
        ]
        if not candidates:
            leftover.append(annotation)
            continue
        assigned[_pick(candidates, postings, annotation, chart)] = annotation

    return assigned, leftover


def _pick(
    candidates: list[int],
    postings: list[dict[str, Any]],
    annotation: GlAnnotation,
    chart: dict[str, dict[str, Any]] | None,
) -> int:
    """Which of several lines on the same account an unlabelled annotation means.

    Honda lists 2320 twice: the vehicle's inventory line at zero, and the DOC
    fee line preset to 380.00. "2320 380" written on the page means the second,
    and the store having typed 380 into that line is the evidence for it --
    taking the first instead overwrote the invoice price with the DOC fee.
    """
    if len(candidates) == 1:
        return candidates[0]

    # A line the store already configured to this exact figure.
    for i in candidates:
        preset = postings[i].get("amount")
        if preset and round(abs(float(preset)), 2) == round(abs(annotation.amount), 2):
            return i

    def plays_a_role(i: int) -> bool:
        # Holdback, floor plan and invoice price are computed from the invoice
        # total. An annotation that did not name one of them should not land on
        # one.
        return bool(
            role_of(
                postings[i].get("description"),
                _account_name(postings[i].get("glAccountId"), chart),
                gl_number=gl_number_of(postings[i].get("glAccountId"), chart),
            )
        )

    def disagrees_with_its_preset(i: int) -> bool:
        # A store that typed 150.00 into a line stated what that line holds.
        # An annotation of 428.00 is evidence about some OTHER line, and putting
        # it here would overwrite a decision with a guess. This is the second
        # guard on Honda's DMA line, which is preset to 150.00 and sits above
        # two 2248 lines left at zero.
        preset = postings[i].get("amount")
        return bool(preset) and round(abs(float(preset)), 2) != round(
            abs(annotation.amount), 2
        )

    for test in (
        lambda i: not plays_a_role(i) and not disagrees_with_its_preset(i),
        lambda i: not plays_a_role(i),
        lambda i: not disagrees_with_its_preset(i),
    ):
        for i in candidates:
            if test(i):
                return i

    return candidates[0]


# How close a reading of a handwritten label has to be to count as that label.
#
# 0.75 is chosen so a three-letter label is still effectively exact -- "HTB"
# against "HUB" scores 0.67 and does not match -- while a longer one tolerates
# the character or two that handwriting costs. "FLOORASUT" against "FLOORASST"
# is 0.89.
_LABEL_THRESHOLD = 0.75


def _label_score(annotation_label: Any, line_description: Any) -> float:
    """How strongly the word beside the account names this template line.

    Not equality. The label is HANDWRITING, and OCR reads the same scrawl
    differently on different runs: this invoice's FLOORASST came back as
    "FLOORASST" one run and "FLOORASUT" the next. Requiring an exact match meant
    the second reading fell through to matching on account number alone, where
    428.00 landed on the DMA line and the real FLOORASST line picked up a
    spurious mirror of the line above it.

    Prefixes score just under exact, which covers the ordinary case of OCR
    reading "FUELALLOW" for "FUELALLOWANCE". Normalising also drops the trailing
    underscore that marks a mirror line, so "HTB" scores full against "HTB_" --
    wanted, because the mirror carries a different ACCOUNT and the pair still
    names exactly one line.

    Three characters minimum, so a stray letter cannot match half a template.
    """
    a = _normalise(annotation_label)
    b = _normalise(line_description)
    if len(a) < 3 or len(b) < 3:
        return 0.0
    if a == b:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def _annotated_amount(
    annotation: GlAnnotation, role: str, preset: float | None
) -> float:
    """The signed amount an annotated line posts.

    The magnitude is always the clerk's. The direction belongs to the role where
    the line has one -- a clerk writes "3300 -> 32,133.00" beside the total but
    the floor plan is credited, and taking that at face value threw the entry
    out by twice the price of the car.

    Failing a role, the minus sign ON THE PAGE decides. Honda's template leaves
    HTB_ and FLOORASST_ at zero, so there is no preset to take a direction from,
    and the "-641.93" the clerk wrote is the only evidence there is.
    """
    if role in _ROLE_SIGN:
        return abs(annotation.amount) * _ROLE_SIGN[role]
    if annotation.signed:
        return annotation.amount
    if preset:
        return abs(annotation.amount) * (-1.0 if preset < 0 else 1.0)
    return abs(annotation.amount)


def _partner_amount(
    partner: dict[str, Any],
    annotation: GlAnnotation,
    chart: dict[str, dict[str, Any]] | None,
) -> float:
    """What the annotated line above posted, so its mirror can oppose it."""
    number = gl_number_of(partner.get("glAccountId"), chart)
    role = role_of(
        partner.get("description"),
        _account_name(partner.get("glAccountId"), chart),
        gl_number=number,
    )
    preset = partner.get("amount")
    return _annotated_amount(
        annotation, role, None if preset is None else round(float(preset), 2)
    )


def _assigned_to_role(
    postings: list[dict[str, Any]],
    assigned: dict[int, GlAnnotation],
    role: str,
    chart: dict[str, dict[str, Any]] | None,
) -> float | None:
    """The amount written against the first template line playing `role`."""
    for i, p in enumerate(postings):
        number = gl_number_of(p.get("glAccountId"), chart)
        if role_of(
            p.get("description"),
            _account_name(p.get("glAccountId"), chart),
            gl_number=number,
        ) == role:
            annotation = assigned.get(i)
            return None if annotation is None else annotation.amount
    return None


# Roles whose direction is fixed by what the account is for, whatever the
# invoice says. The floor plan is money the dealership owes, so it is always a
# credit; holdback and inventory are things it owns, so always debits.
_ROLE_SIGN = {
    ROLE_FLOOR_PLAN: -1.0,
    ROLE_HOLDBACK: 1.0,
    ROLE_INVOICE_PRICE: 1.0,
}


def _sign_for(role: str, preset: float | None) -> float:
    """Which direction a line posts in.

    The role decides where it can. Failing that the template's own preset shows
    the store's intent -- a line configured at -150.00 is a credit line even
    when this invoice puts a different number in it. Everything else debits.
    """
    if role in _ROLE_SIGN:
        return _ROLE_SIGN[role]
    if preset:
        return -1.0 if preset < 0 else 1.0
    return 1.0


def _account_name(gl_account_id: Any, chart: dict[str, dict[str, Any]] | None) -> str:
    if not chart:
        return ""
    entry = chart.get(str(gl_account_id or "")) or {}
    return str(entry.get("account_name") or "")


def _account_for_role(
    postings: list[dict[str, Any]], role: str, chart: dict[str, dict[str, Any]] | None
) -> str:
    """The GL number of the template line playing `role`, or "" if none does."""
    for p in postings:
        number = gl_number_of(p.get("glAccountId"), chart)
        if role_of(
            p.get("description"),
            _account_name(p.get("glAccountId"), chart),
            gl_number=number,
        ) == role:
            return number
    return ""
