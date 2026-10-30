"""Per-line GL classification for vendor stock orders.

WHAT THIS DOES
    A vendor stock invoice is a list of parts, and a dealership does not keep
    them all in one inventory account. Tires, oil and everything else land in
    different places. This reads each line item and decides which account its
    money belongs in, so the pre-invoice posts a real breakdown rather than one
    lump sum.

    The accounts, per the parts inventory scheme:

        2410 / 2411 / 2412   parts primary - general parts inventory
        2430                 tires and tire-related hardware
        2440                 oil, fluids and lubricants

    Which of those a category maps to varies by store - Oakbrook Toyota puts
    tires in 2411 and oil in 2430, where Schaumburg Honda uses 2430 and 2440 -
    so the mapping is per dealership with a conservative default.

HOW IT DECIDES
    One Gemini call classifies every line on the invoice at once, rather than
    one call per line. That is cheaper, and it lets the model see the whole
    invoice: "FIRST DEFENSE KT" is ambiguous alone but obvious on a sheet of
    Valvoline fluids.

    The model only ever returns a CATEGORY. It never picks a GL account - the
    account comes from the table below, keyed on the dealership. That keeps the
    accounting decision in code that can be read and corrected, and follows what
    gl_service.py already does for the other flows.

WHY THIS IS NOT SIMPLY TRUSTED
    Tekion returns its own per-line postings from `preInvoicing/postings`, drawn
    from each part's inventory setup, and those already sum exactly to the
    invoice. Replacing them with a classification is only safe when the numbers
    still add up, so `reconcile()` checks the split total against the invoice
    before anything is sent. When it does not reconcile - OCR missed a line,
    read a price wrong, or the invoice carries freight - the caller is expected
    to fall back to Tekion's own postings rather than post a guess.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

PROJECT_ROOT = MAIN_PIPELINE.parent
if not os.environ.get("VERTEX_CREDENTIALS"):
    _creds = PROJECT_ROOT / "neura_vertex_ai.json"
    if _creds.exists():
        os.environ["VERTEX_CREDENTIALS"] = str(_creds)

from google.genai import types  # noqa: E402

from api.services.vendor_service import _normalize  # noqa: E402
from pipeline import get_client  # noqa: E402

# Small and fast: this is a three-way choice per line, not extraction.
CLASSIFIER_MODEL = "gemini-2.5-flash-lite"

TIRES = "TIRES"
OIL_FLUIDS = "OIL_FLUIDS"
GENERAL_PARTS = "GENERAL_PARTS"
CATEGORIES = (TIRES, OIL_FLUIDS, GENERAL_PARTS)

# Parts primary is the safe landing place: it is where general inventory goes,
# so a line nobody could classify ends up in the broadest correct account
# rather than in tires or oil where it would be plainly wrong.
DEFAULT_CATEGORY = GENERAL_PARTS

# -- Category to account, per dealership --------------------------------------
# Mirrors _STOCK_GL in gl_service.py, which the other flows already use. Kept
# separate rather than imported so a per-line change here cannot alter the
# whole-PO classification those flows depend on.

_DEFAULT_GL: dict[str, str] = {
    TIRES: "2430",
    OIL_FLUIDS: "2440",
    GENERAL_PARTS: "2410",
}

_DEALERSHIP_GL: dict[str, dict[str, str]] = {
    "SCHAUMBURG KIA": {TIRES: "2410", OIL_FLUIDS: "2440", GENERAL_PARTS: "2410"},
    "SCHAUMBURG FORD": {TIRES: "2410", OIL_FLUIDS: "2440", GENERAL_PARTS: "2410"},
    "SCHAUMBURG HONDA": {TIRES: "2430", OIL_FLUIDS: "2440", GENERAL_PARTS: "2410"},
    "OAKBROOK TOYOTA IN WESTMONT": {TIRES: "2411", OIL_FLUIDS: "2430", GENERAL_PARTS: "2410"},
}


def gl_for(category: str, dealership_name: str) -> str:
    """The bare account number for a category at one store, e.g. "2440"."""
    table = _DEALERSHIP_GL.get(_normalize(dealership_name or ""), _DEFAULT_GL)
    return table.get(category, _DEFAULT_GL[DEFAULT_CATEGORY])


_PROMPT = """You are a dealership parts inventory assistant. Classify EVERY line item on this
vendor invoice into the inventory category its cost belongs to.

Categories:
- TIRES - tires, wheels, rims, and tire-specific hardware (valve stems, wheel weights, TPMS sensors)
- OIL_FLUIDS - motor oil, transmission fluid, coolant, antifreeze, DEF, grease, lubricants, brake
  fluid, power steering fluid, and fluid-system cleaners, flushes and additives
- GENERAL_PARTS - everything else: brake pads, filters, belts, hardware, body parts, electrical,
  shop supplies, and anything you are unsure about

Rules:
- Return exactly one entry for EVERY line, using the index given. Do not skip, merge or reorder.
- Judge by what the part IS, not by the vendor. An oil supplier still sells wiper blades.
- Use the whole invoice as context when a description is abbreviated or truncated.
- When genuinely unsure, answer GENERAL_PARTS.

Line items:
{lines}"""


def _schema() -> types.Schema:
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "lines": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "index": types.Schema(type=types.Type.INTEGER),
                        "category": types.Schema(
                            type=types.Type.STRING, enum=list(CATEGORIES)
                        ),
                    },
                    required=["index", "category"],
                ),
            )
        },
        required=["lines"],
    )


@dataclass
class ClassifiedLine:
    """One invoice line and the account its cost was assigned to."""

    index: int
    description: str
    amount: float
    category: str
    # Bare account number, e.g. "2440". Prefixed with the dealer id when posted.
    gl_account: str
    # True when the model did not classify this line and the default was used.
    defaulted: bool = False

    def __str__(self) -> str:
        mark = " (defaulted)" if self.defaulted else ""
        return f"{self.description[:44]:44s} {self.amount:>10.2f}  {self.gl_account}{mark}"


@dataclass
class Classification:
    lines: list[ClassifiedLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    def by_account(self) -> dict[str, float]:
        """Totals per account - several lines usually share one."""
        out: dict[str, float] = {}
        for line in self.lines:
            out[line.gl_account] = round(out.get(line.gl_account, 0.0) + line.amount, 2)
        return out

    def reconcile(self, expected: float, tolerance: float = 0.005) -> bool:
        """Do the classified lines add up to what is being invoiced?

        Tekion posts the expense side and the AP side as one balanced entry. If
        these lines do not sum to the invoice, the entry does not balance and
        the post is either rejected or silently wrong - so the caller must not
        use the splits when this is False.
        """
        return abs(self.total - expected) <= tolerance

    def to_gl_splits(self, dealer_id: str, merge: bool = True) -> list[dict[str, Any]]:
        """The `gl_splits` payload for TekionApiClient.pre_invoice().

        Merged by account by default: Tekion wants the accounting breakdown, and
        eleven lines that all land in 2440 are one posting of the total, not
        eleven identical postings.
        """
        if merge:
            return [
                {
                    "gl_account_id": f"{dealer_id}_{account}",
                    "amount": amount,
                    "description": None,
                }
                for account, amount in sorted(self.by_account().items())
            ]
        return [
            {
                "gl_account_id": f"{dealer_id}_{line.gl_account}",
                "amount": line.amount,
                "description": line.description or None,
            }
            for line in self.lines
        ]


def _describe(line_items: list[dict[str, Any]]) -> str:
    rows = []
    for i, item in enumerate(line_items):
        part = str(item.get("partNumber") or item.get("part_number") or "").strip()
        desc = str(item.get("description") or "").strip() or "(no description)"
        label = f"{desc} [{part}]" if part else desc
        rows.append(f"{i}. {label}")
    return "\n".join(rows)


def classify_line_items(
    line_items: list[dict[str, Any]], dealership_name: str
) -> Classification:
    """Assign every line item an inventory account.

    Never raises: a failed call classifies everything as general parts, which
    is where an unclassified part belongs anyway. The caller still has to
    reconcile() before trusting the result.
    """
    result = Classification()
    priced = [
        (i, item)
        for i, item in enumerate(line_items)
        if float(item.get("totalPrice") or item.get("total_price") or 0) != 0
    ]
    if not priced:
        result.notes.append("No priced line items were read from the invoice.")
        return result

    verdicts: dict[int, str] = {}
    try:
        client = get_client()
        resp = client.models.generate_content(
            model=CLASSIFIER_MODEL,
            contents=_PROMPT.format(lines=_describe([item for _, item in priced])),
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_schema(),
                max_output_tokens=4096,
            ),
        )
        for entry in json.loads(resp.text or "{}").get("lines") or []:
            category = str(entry.get("category") or "").strip().upper()
            if category in CATEGORIES:
                verdicts[int(entry.get("index"))] = category
    except Exception as e:  # noqa: BLE001 - a classifier outage must not stop an invoice
        result.notes.append(f"Classification failed ({e}); every line treated as general parts.")

    for position, (original_index, item) in enumerate(priced):
        category = verdicts.get(position)
        result.lines.append(
            ClassifiedLine(
                index=original_index,
                description=str(item.get("description") or "").strip(),
                amount=round(
                    float(item.get("totalPrice") or item.get("total_price") or 0), 2
                ),
                category=category or DEFAULT_CATEGORY,
                gl_account=gl_for(category or DEFAULT_CATEGORY, dealership_name),
                defaulted=category is None,
            )
        )

    missing = sum(1 for line in result.lines if line.defaulted)
    if missing and not result.notes:
        result.notes.append(
            f"{missing} of {len(result.lines)} lines were not classified and "
            "defaulted to general parts."
        )
    return result
