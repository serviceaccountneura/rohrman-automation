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
"""
from __future__ import annotations

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
_ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ROLE_HOLDBACK, ("holdback", "holdbk")),
    (ROLE_FLOOR_PLAN, ("floorplan", "notepayable", "npnewvehicle", "npnew")),
    (ROLE_INVOICE_PRICE, ("invoiceprice", "newinv", "inventory")),
)


def _normalise(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def role_of(*texts: Any) -> str:
    """Which role a line plays, from any text that describes it.

    Takes several candidates because a template's own `description` is usually
    null -- Schaumburg Kia's BILL has none at all -- and the GL account NAME
    from the chart is what actually says what the line is for:
    "N/P NEW VEHICLE & DEMOS" is the floor plan, "NEW INV - KIA" the inventory.
    """
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
    # Accounts the clerk annotated that the template has no line for. Not fatal
    # on its own, but it means the entry will not carry money the person
    # intended to place, so callers should refuse rather than post a short one.
    unmatched_annotations: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def fill(
    template: dict[str, Any],
    annotations: dict[str, float],
    dealer_cost_total: float,
    chart: dict[str, dict[str, Any]] | None = None,
) -> FillResult:
    """Fill one Tekion template from one invoice's annotations.

    `template` is a raw template object from
    POST /api/accounting/u/v2/transaction/upc/templates.
    `annotations` maps a GL account number to the positive amount written
    against it. `chart` maps glAccountId to the account's own record, and is
    what turns a template line's opaque id into the number the clerk wrote.
    """
    result = FillResult()
    postings = template.get("postings") or []
    used: set[str] = set()
    # Roles are one-shot: see the note where they are applied.
    roles_filled: set[str] = set()

    # A template line's sign is carried by its preset amount where it has one,
    # and by its role otherwise. Mirror lines are matched by their partner's
    # description with a trailing underscore, which is the Tekion convention.
    by_description: dict[str, list[dict[str, Any]]] = {}
    for p in postings:
        by_description.setdefault(str(p.get("description") or ""), []).append(p)

    holdback = annotations.get(_account_for_role(postings, ROLE_HOLDBACK, chart), None)

    for p in postings:
        gl = gl_number_of(p.get("glAccountId"), chart)
        description = str(p.get("description") or "")
        account_name = _account_name(p.get("glAccountId"), chart)
        preset = p.get("amount")
        preset = None if preset is None else round(float(preset), 2)
        role = role_of(description, account_name)

        amount: float | None = None
        source = ""

        # 1. Written on the invoice against this exact account.
        #
        # The annotation supplies the MAGNITUDE only. Sign belongs to the line,
        # not to the handwriting: a clerk writes "3300 -> 32,133.00" beside the
        # total, but the floor plan account is credited, and taking the written
        # number at face value posted it as a debit and threw the entry out by
        # twice the price of the car.
        if gl in annotations:
            amount = abs(annotations[gl]) * _sign_for(role, preset)
            source = f"annotated {gl}"
            used.add(gl)

        # 2. A mirror of a line that was annotated: "DMA_" follows "DMA".
        elif description.endswith("_") and description[:-1] in by_description:
            partner = by_description[description[:-1]][0]
            partner_gl = gl_number_of(partner.get("glAccountId"), chart)
            if partner_gl in annotations:
                # The mirror always opposes its partner, which is the whole
                # point of the pair: DMA 150.00 against DMA_ -150.00.
                amount = -abs(annotations[partner_gl]) * _sign_for(
                    role_of(
                        partner.get("description"),
                        _account_name(partner.get("glAccountId"), chart),
                    ),
                    partner.get("amount"),
                )
                source = f"mirrors {partner_gl}"

        # 3. A role the template describes, computed from the invoice.
        #
        # Only the FIRST line playing a role takes it. BILL lists 2320 NEW INV
        # twice -- once for the vehicle itself and once for the DOC fee -- and
        # filling both from the same rule posted the price of the car twice.
        if amount is None and role and dealer_cost_total and role not in roles_filled:
            if role == ROLE_FLOOR_PLAN:
                amount = -dealer_cost_total
                source = "dealer cost (credit)"
                roles_filled.add(role)
            elif role == ROLE_INVOICE_PRICE and holdback is not None:
                amount = round(dealer_cost_total - holdback, 2)
                source = "dealer cost less holdback"
                roles_filled.add(role)

        # 4. Whatever the store preset in the template.
        if amount is None and preset:
            amount = preset
            source = "template preset"

        if amount is None or round(amount, 2) == 0.0:
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

    result.unmatched_annotations = {
        gl: amount for gl, amount in annotations.items() if gl not in used
    }
    return result


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
        if role_of(p.get("description"), _account_name(p.get("glAccountId"), chart)) == role:
            return gl_number_of(p.get("glAccountId"), chart)
    return ""
