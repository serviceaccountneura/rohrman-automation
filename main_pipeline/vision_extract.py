#!/usr/bin/env python3
"""
vision_extract.py — VISION-FIRST extraction + test runner.

Unlike pipeline.py (which OCRs the image to plain text, then asks a SECOND model
call to re-structure that text — losing tables and any value the OCR dropped),
this script sends the page image(s) DIRECTLY to Gemini and gets back structured
JSON — tables included — in ONE vision-grounded pass with a strict response
schema. No lossy text middle step, so far fewer missing/invented values.

USAGE
    python vision_extract.py                      # process every PDF/image in this folder
    python vision_extract.py --all                # same
    python vision_extract.py "<file.pdf>"         # one file
    python vision_extract.py a.pdf b.png          # several files
    python vision_extract.py --model gemini-2.5-flash   # override model (default: pro)
    python vision_extract.py --out results        # write full JSON to results/<name>.json

Needs Vertex creds:  export VERTEX_CREDENTIALS=/abs/path/to/service-account.json
                     (or place neura_vertex_ai.json next to these files)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google.genai import types

# Reuse the existing, tested helpers — don't duplicate them.
from pipeline import (
    BASE_DIR,
    IMAGE_EXTS,
    MODEL as DEFAULT_MODEL,
    get_client,
    load_pages,
    pil_to_part,
)
from normalize import to_number, to_po_contract

# Vision-first works best on a strong model; the whole point is fixing accuracy.
VISION_MODEL = "gemini-2.5-pro"

# --------------------------------------------------------------------------- #
# Single-pass prompt: image(s) -> structured JSON (tables included).
# Carries over the hard-won handwriting/faithfulness guidance from pipeline.py's
# OCR_PROMPT and doc_schema.py's STRUCTURE_PROMPT, but grounded on the IMAGE.
# --------------------------------------------------------------------------- #
VISION_PROMPT = """You are an expert document-understanding engine for automotive dealership paperwork
(vendor invoices, purchase orders, parts statements, journal/posting entries, receipts). You read
DIFFICULT HANDWRITING as well as printed text, directly from the page image(s).

Extract EVERYTHING on the page(s) into the provided JSON schema. Work directly from the image —
do not skip anything.

READING (printed + handwriting):
- Examine handwriting stroke by stroke before committing. Messy, cursive, faint or overlapping
  writing must still be read, not skipped.
- Use context to resolve ambiguous characters: a "Qty" cell is a number; a date field follows a
  date pattern; a column of figures should arithmetically agree with its total. Watch classic
  confusions (0/O, 1/7/l/I, 2/Z, 5/S, 6/G, 8/B) and pick the reading that keeps totals consistent.

TABLES (most important — reproduce them faithfully):
- Put EVERY table into `tables[]` exactly as printed: capture the real column headers in order in
  `columns`, and one entry per data row in `rows` (a list of cell strings, same length/order as
  `columns`). Keep empty cells as "". Never merge two columns, never drop a column, never invent a
  row. If a table continues across pages, merge its rows into ONE table entry — do not repeat the
  header as a data row.
- ALSO populate the typed helpers where they apply: `line_items[]` for part/qty/price rows,
  `totals[]` for subtotal/tax/total/balance figures, `identifiers[]` for invoice/PO/control/account
  numbers and dates. These may duplicate data already in `tables[]` — that is fine and expected.

NUMBERS & FAITHFULNESS:
- Clean currency of symbols and commas into the string value where a number is expected
  ("$1,250.00" stays readable but represents 1250.00; "-$376.00" is negative). Preserve negatives.
- Use null / "" for blanks and dashes. NEVER invent a value to fill a field.
- If a token is genuinely unreadable, add it to `illegible[]` rather than guessing.
- Capture handwritten annotations, margin notes, stamps, signatures and initials in
  `handwritten_notes[]` (note signatures as "[signature] <legible name>").

MULTI-DOCUMENT UPLOADS:
- One upload may be a single document spanning pages, OR a PRIMARY document plus SUPPORTING
  documents (e.g. a dealership PO on page 1, then the vendor's final bill on later pages). Keep
  each document's totals SEPARATE — never mix amounts from different documents. Put the primary
  document's fields at the top level; capture each supporting document's table(s) as their own
  entries in `tables[]` with a descriptive `title`.
- Ignore page chrome: repeated letterheads, "Page 1 of 2", "Dealer Copy", print timestamps, QR
  codes. Reconcile a value that appears on multiple pages into a single reading.

GL ACCOUNTS & SPATIAL BINDING:
- DETECT ALL GL CODES: Locate any printed or handwritten General Ledger account codes anywhere on
  the page (typically 4-digit or 5-digit numeric codes, often appearing alongside terms or symbols
  like "GL", "Acct", "Account", "#", or descriptive tags like "Credit", "Tires", "Supplies"). Do
  not limit detection to predefined numbers.
- BIND GL CODES TO CHARGES (LAYOUT-AGNOSTIC SPATIAL RULES):
  Use the visual layout and handwritten annotations to map each detected GL code to its
  corresponding line item, charge, or fee:
  1. Horizontal Row Alignment (HIGHEST PRIORITY, CHECK THIS FIRST): If a GL code sits on the same
     horizontal band as a line item -- including in the LEFT OR RIGHT MARGIN, outside the table
     border, level with that row -- it belongs to THAT line item and no other. Being in the margin
     does not make it a document-wide default; only being level with no row does. A code written
     level with the first of ten rows applies to that row alone.
     Different rows routinely carry different codes: read each one independently and give every
     row its own `gl_account`.
  2. Vector / Arrow Anchoring: If a line or arrow points from a GL code to a specific cell, part
     description, or amount, bind the GL code to that target item regardless of where it appears.
     THE TARGET MAY BE INSIDE A SENTENCE, not only in a table. Vehicle invoices state figures in
     prose -- "DEALER RECEIVES A RESERVE OF $1,129.00, WHICH INCLUDES A PPO RESERVE OF $392.00,
     AND WHOLESALE FINANCE RESERVE OF $368.00".

     FOLLOW THE ARROW BY POSITION. An arrow drawn above a line of text points at whatever is
     DIRECTLY BELOW ITS TIP -- compare horizontal positions and take the figure the tip lands on
     or nearest to it. Do not pick a figure because its wording sounds related to the account, and
     do not pick the first or last figure in the sentence by default. Where two arrows sit above
     the same sentence they land at different horizontal positions and therefore on different
     figures; the LEFTMOST arrow takes the leftmost figure it reaches, the next arrow the next.

     On the invoice above, an arrow whose tip sits over "$1,129.00" means $1,129.00 -- not the
     $392.00 or $368.00 later in the same sentence.
  2a. TRANSCRIBE BEFORE YOU DECIDE. For every handwritten GL code with an arrow, first read out
     the text the arrow TIP physically touches -- roughly 40 characters of the line directly
     beneath it, copied verbatim -- and put that in `mapped_description`. Then take `amount` from
     the money figure inside THAT span, and nowhere else.

     This is a transcription task, not a judgement: copy what is under the tip and read its
     number. Do not summarise the clause, do not name the kind of figure it is, and do not choose
     between figures elsewhere in the sentence because their wording suits the account.

     A sentence may hold several figures -- "A RESERVE OF $1,129.00, WHICH INCLUDES A PPO RESERVE
     OF $392.00, AND WHOLESALE FINANCE RESERVE OF $368.00" holds three. Two handwritten codes do
     NOT mean the last two figures, or the first two. Each code takes the figure its own arrow
     lands on, and two codes may well land on figures that are not adjacent.

  2b. NEVER invent the pairing. If a GL code is written but you cannot determine which figure its
     arrow lands on, report the code in `gl_mappings[]` with `"amount": null` and say in
     `mapped_description` what was unclear. Guessing from wording is what produces a confident
     wrong answer: an account named for one thing is routinely pointed at a figure described as
     another, and only the arrow says which. A wrong amount is far worse than a missing one: it
     posts real money to a real account and balances, so nothing downstream can detect it.
  3. Enclosure & Contour Grouping: If a dollar amount, line item, fee, discount, or tax line is
     circled, boxed, or underlined, and a GL code is written inside or adjacent to that boundary,
     bind the GL code exclusively to that enclosed item and amount.
  4. Unanchored / Global Fallback (LAST RESORT): Only when a GL code lines up with no row at all
     -- in a page header, footer, or a blank area well away from the table -- treat it as the
     default for items that got no code from rules 1-3. Never apply this to a code that is level
     with a row; that is rule 1.
  5. Multi-GL Split Handling: If an invoice contains multiple GL codes, resolve each code's
     spatial target independently to ensure every line item, subtotal, discount, or extra fee is
     assigned its correct GL code and corresponding dollar amount.
- POPULATE OUTPUT:
  - For items in `line_items[]`, populate the string field `gl_account` (e.g. `gl_account: "2410"`)
    on EVERY row that has one. This is per-row: three rows with three different handwritten codes
    must come back as three different `gl_account` values, not one repeated or one at document
    level. Leave it empty only for a row with no code of its own.
  - For fees, discounts, freight, or subtotals outside the main table that have an assigned GL
    code, populate `gl_mappings[]` with: `gl_account`, `amount`, and `mapped_description`
    (e.g. `{"gl_account": "7555", "amount": "15.12", "mapped_description": "Delivery Charge"}`).
  - GL CODES WRITTEN WITH THEIR OWN AMOUNTS: wherever an account appears with a dollar figure
    beside it, that is the clerk stating how the invoice divides. It may be circled, boxed, in a
    margin, at the foot of the page, or just written plainly with nothing around it -- the layout
    does not matter and there is no need for it to look like a block. What identifies it is an
    account number followed by an amount. The commonest form is a short list written in open
    space on the page:

        GL 2245    1129$
        GL 2250     368$

    There is no limit to how many lines such a list has, and the prefix varies -- "GL", "GL#",
    "gl", or nothing at all before the number. Capture EVERY such pair in `gl_mappings[]`, one
    entry each, with the amount exactly as written:

        GL# 7193   $2,378.11        -> {"gl_account": "7193", "amount": "2378.11", ...}
        GL# 3142   $202.12          -> {"gl_account": "3142", "amount": "202.12", ...}

    Accounts written this way do NOT belong to any single row and must not be copied into
    `line_items[].gl_account`. This is now the usual way GL accounts are marked: expect the
    accounts to be written once for the invoice with their amounts, NOT beside individual rows.
    They typically split the invoice total -- commonly goods against sales tax -- so the amounts
    are expected to sum to it. Read the account from the "GL#"/"GL" label and the amount from the
    figure beside it; a superscript or raised cent figure ($202^12) is 202.12.
  - A code in that block that appears WITHOUT an amount is the older per-row style: leave it out of
    the block and bind it to its row under the spatial rules above.
  - ALWAYS populate `gl_mappings[]` for a handwritten GL code that an arrow, line or bracket ties
    to ANY value on the page -- not only for fees and charges. On a vehicle manufacturer invoice
    this is the entire point of the annotation, and every handwritten code must appear there.
  - AMOUNTS EMBEDDED IN REFERENCE CODES: the value an arrow points at is often INSIDE a printed
    code rather than shown as a dollar figure. In `1001948819-KAC0780KAC-KRS0290-FPA0156`,
    `KAC0780KAC` carries 780.00 and `KRS0290` carries 290.00: read the digit run beside the
    letters and drop leading zeros. When a handwritten GL code points at such a segment, emit
    `{"gl_account": "2245", "amount": "780.00", "mapped_description": "KAC0780KAC"}` -- put the
    code segment verbatim in `mapped_description` so the reading can be checked.
  - A REPAIR ORDER NUMBER WRITTEN ON A ROW: a sublet invoice often bills several vehicles at
    once, one per row, with that vehicle's repair order number written beside it -- frequently by
    hand, in a different colour from the printed text, and often with a stock number on the same
    row. Put it in that row's `ro_number`, per row, exactly as the per-row `gl_account` works:
    eight rows with eight different numbers must come back as eight different `ro_number` values,
    not one repeated and not one at document level.

    An RO number is 5-8 digits and carries NO letters. Do not put a stock number there -- those
    look like "SH3626P" or "SH10649A", letters and digits together -- and do not put a part
    number, a VIN fragment, a date or a price there. A row with no such number gets an empty
    `ro_number`; leave it out rather than reusing the row above.
  - A PURCHASE ORDER NUMBER WRITTEN ON BY HAND: staff often write the PO number the invoice
    should be billed against in a margin or at the top of the page -- "PO 35096", "P.O. #35096",
    or just "35096" beside the word PO. Put it in `identifiers[]` as
    `{"label": "Purchase Order", "value": "35096"}`, exactly as a printed one would be, AND leave
    it in `handwritten_notes[]` as well. It is the same fact whether it was typed or written.

    Do NOT report the vendor's postal "PO BOX" as a purchase order number, and do not invent one
    from an invoice number, account number or RO number that happens to sit near the word PO.
  - FIGURES PRINTED IN CENTS WITH NO DECIMAL POINT: a vehicle manufacturer invoice often prints a
    short run of bare digit groups on or beside the MSRP line, with no dollar sign, no comma and
    no decimal point:

        MSRP $42,795.00      42800   4400   85590

    These are amounts in cents -- 428.00, 44.00 and 855.90 -- and they are the manufacturer's own
    statement of the allowances and holdback on the car. Copy each one into `coded_amounts[]`
    EXACTLY as printed, as a digit string, in left-to-right order: `["42800", "4400", "85590"]`.
    Do not insert the decimal point, do not reorder them and do not drop one because it looks
    like a duplicate of something written by hand.

    Only bare digit runs belong here. Leave out anything carrying a dollar sign, comma or decimal
    point (the MSRP itself), and leave out the VIN, engine number, stock number, control number,
    key code, dealer number, order reference, zip code and phone number. If the invoice has no
    such run, return an empty array.
  - When a handwritten GL code points instead at a labelled figure in a totals column, use that
    figure and name the label (e.g. `{"gl_account": "3300", "amount": "32133.00",
    "mapped_description": "TOTAL dealer cost"}`).
  - Report `amount` as a POSITIVE number in `gl_mappings[]`. Debit/credit direction is decided
    downstream, never here.

Return ONLY the JSON object described by the schema. No commentary, no markdown fences.
"""

# --------------------------------------------------------------------------- #
# Strict response schema (flexible top-level + faithful generic tables).
# --------------------------------------------------------------------------- #
_S = types.Schema
_T = types.Type


def _obj(props: dict, required: list[str] | None = None) -> types.Schema:
    return _S(type=_T.OBJECT, properties=props, required=required or [])


def _arr(items: types.Schema) -> types.Schema:
    return _S(type=_T.ARRAY, items=items)


def _str() -> types.Schema:
    return _S(type=_T.STRING, nullable=True)


def build_response_schema() -> types.Schema:
    label_value = _obj({"label": _str(), "value": _str()})
    return _obj({
        "document_type": _str(),
        "vendor": _obj({
            "id": _str(), "name": _str(), "address": _str(), "phone": _str(),
        }),
        "dealership": _obj({"name": _str(), "address": _str()}),
        "identifiers": _arr(label_value),
        "vehicle": _obj({
            "year_make_model": _str(), "vin": _str(), "mileage": _str(),
        }),
        "line_items": _arr(_obj({
            "line_no": _str(),
            "part_number": _str(),
            "description": _str(),
            "qty": _str(),
            "unit_price": _str(),
            "total_price": _str(),
            "gl_account": _str(),
            "ro_number": _str(),
        })),
        "tables": _arr(_obj({
            "title": _str(),
            "columns": _arr(_S(type=_T.STRING)),
            "rows": _arr(_arr(_S(type=_T.STRING))),
        })),
        "totals": _arr(label_value),
        "gl_mappings": _arr(_obj({
            "gl_account": _str(),
            "amount": _str(),
            "mapped_description": _str(),
        })),
        "coded_amounts": _arr(_S(type=_T.STRING)),
        "handwritten_notes": _arr(_S(type=_T.STRING)),
        "illegible": _arr(_S(type=_T.STRING)),
    }, required=["document_type"])


# --------------------------------------------------------------------------- #
# Validation — schema-aware (works on THIS script's structured shape, where
# numbers live as strings in totals[]/line_items[] rather than numeric keys).
# Ledgers must balance (debit==credit, balance≈0); invoices/POs should have
# line items that add up to the stated total.
# --------------------------------------------------------------------------- #
def _totals_map(doc: dict) -> dict:
    """{lowercased label: number} from totals[] (e.g. {'debit': 64.26})."""
    out = {}
    for t in (doc.get("totals") or []):
        label = str(t.get("label") or "").strip().lower()
        num = to_number(t.get("value"))
        if label and num is not None:
            out[label] = num
    return out


def _pick(totals: dict, *names) -> float | None:
    for n in names:
        for label, num in totals.items():
            if n in label:
                return num
    return None


def validate(doc: dict) -> dict:
    checks, failed = {}, []
    totals = _totals_map(doc)
    dt = str(doc.get("document_type") or "").upper()
    is_ledger = any(t in dt for t in ("JOURNAL", "POSTING", "LEDGER"))

    if is_ledger:
        debit, credit = _pick(totals, "debit"), _pick(totals, "credit")
        if debit is not None and credit is not None:
            ok = abs(debit - credit) < 0.01
            checks["debit_equals_credit"] = ok
            if not ok:
                failed.append(f"debit ({debit:.2f}) != credit ({credit:.2f})")
        balance = _pick(totals, "balance")
        if balance is not None:
            ok = abs(balance) < 0.01
            checks["balance_is_zero"] = ok
            if not ok:
                failed.append(f"balance ({balance:.2f}) != 0")
    else:
        items = [to_number(it.get("total_price")) for it in (doc.get("line_items") or [])]
        items = [x for x in items if x is not None]
        stated = _pick(totals, "grand total", "amount due", "balance due", "total")
        if items and stated is not None:
            s = sum(items)
            ok = abs(s - stated) <= max(0.02, abs(stated) * 0.01)
            checks["line_items_match_total"] = ok
            if not ok:
                failed.append(f"line items ({s:.2f}) != total ({stated:.2f})")

    return {
        "checks": checks,
        "needs_review": bool(failed),
        "notes": ("Validation issues: " + ", ".join(failed)) if failed else None,
    }


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def extract_vision(path: Path, client, model: str) -> dict:
    """One vision call over all pages of `path` -> rich structured dict."""
    images = load_pages(path)
    parts = [pil_to_part(img) for img in images]
    parts.append(types.Part.from_text(text=VISION_PROMPT))

    resp = client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=build_response_schema(),
            max_output_tokens=65535,
        ),
    )
    try:
        doc = json.loads(resp.text or "{}")
    except json.JSONDecodeError:
        doc = {"document_type": None, "_parse_error": (resp.text or "")[:500]}
    if not isinstance(doc, dict):
        doc = {"document_type": None}

    doc["_pages"] = len(images)
    # "again make structured JSON": fold the rich output into the stable PO contract
    # the Tekion fill scripts already consume.
    doc["_po_contract"] = to_po_contract(doc)
    doc["_validation"] = validate(doc)
    doc["_needs_review"] = doc["_validation"]["needs_review"]
    return doc


# --------------------------------------------------------------------------- #
# Pretty console display
# --------------------------------------------------------------------------- #
def _c(s) -> str:
    return "" if s is None else str(s)


def _cell(v) -> str:
    """Display value with internal newlines collapsed so table columns stay aligned."""
    return " / ".join(part.strip() for part in _c(v).splitlines() if part.strip()) or _c(v)


def _render_table(columns: list, rows: list, indent: str = "    ") -> str:
    cols = [_cell(c) for c in (columns or [])]
    body = [[_cell(v) for v in (r or [])] for r in (rows or [])]
    width = max([len(cols)] + [len(r) for r in body] or [0])
    cols += [""] * (width - len(cols))
    for r in body:
        r += [""] * (width - len(r))
    if width == 0:
        return indent + "(empty)"
    w = [max(len(cols[i]), *(len(r[i]) for r in body)) if body else len(cols[i])
         for i in range(width)]
    def line(cells):
        return indent + " | ".join(c.ljust(w[i]) for i, c in enumerate(cells))
    out = [line(cols), indent + "-+-".join("-" * w[i] for i in range(width))]
    out += [line(r) for r in body]
    return "\n".join(out)


def display(path: Path, doc: dict) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{path.name}   ({doc.get('_pages', '?')} page(s))\n{bar}")
    print(f"document_type : {_c(doc.get('document_type'))}")

    vendor = doc.get("vendor") or {}
    if any(vendor.values()):
        print(f"vendor        : {_c(vendor.get('name'))}"
              f"{('  [' + _c(vendor.get('id')) + ']') if vendor.get('id') else ''}")
        if vendor.get("address") or vendor.get("phone"):
            print(f"                {_c(vendor.get('address'))}  {_c(vendor.get('phone'))}".rstrip())

    dealer = doc.get("dealership") or {}
    if any(dealer.values()):
        print(f"dealership    : {_c(dealer.get('name'))}  {_c(dealer.get('address'))}".rstrip())

    vehicle = doc.get("vehicle") or {}
    if any(vehicle.values()):
        print(f"vehicle       : {_c(vehicle.get('year_make_model'))}  "
              f"VIN={_c(vehicle.get('vin'))}  mi={_c(vehicle.get('mileage'))}".rstrip())

    for ident in (doc.get("identifiers") or []):
        if ident.get("label") or ident.get("value"):
            print(f"  - {_c(ident.get('label'))}: {_c(ident.get('value'))}")

    line_items = doc.get("line_items") or []
    if line_items:
        print(f"\nLINE ITEMS ({len(line_items)}):")
        cols = ["line_no", "part_number", "description", "qty", "unit_price", "total_price"]
        rows = [[it.get(c) for c in cols] for it in line_items]
        print(_render_table(cols, rows))

    for i, tbl in enumerate(doc.get("tables") or [], 1):
        title = _c(tbl.get("title")) or f"table {i}"
        print(f"\nTABLE: {title}  ({len(tbl.get('rows') or [])} row(s))")
        print(_render_table(tbl.get("columns"), tbl.get("rows")))

    totals = doc.get("totals") or []
    if totals:
        print("\nTOTALS:")
        for t in totals:
            print(f"    {_c(t.get('label')):<24} {_c(t.get('value'))}")

    notes = doc.get("handwritten_notes") or []
    if notes:
        print("\nHANDWRITTEN NOTES:")
        for n in notes:
            print(f"    - {_c(n)}")

    illegible = doc.get("illegible") or []
    if illegible:
        print(f"\n⚠ ILLEGIBLE ({len(illegible)}): " + "; ".join(_c(x) for x in illegible))

    val = doc.get("_validation") or {}
    checks = val.get("checks") or {}
    if doc.get("_needs_review"):
        flag = "⚠ NEEDS REVIEW"
    elif checks:
        flag = "✓ checks passed"
    else:
        flag = "– no checks applicable"
    print(f"\nVALIDATION: {flag}   checks={checks}")
    if val.get("notes"):
        print(f"            {val['notes']}")


# --------------------------------------------------------------------------- #
# CLI / batch runner
# --------------------------------------------------------------------------- #
def discover_files() -> list[Path]:
    exts = {".pdf"} | IMAGE_EXTS
    return sorted(p for p in BASE_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in exts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Vision-first Gemini extraction + test runner.")
    ap.add_argument("files", nargs="*", help="files to process (default: all PDFs/images here)")
    ap.add_argument("--all", action="store_true", help="process every PDF/image in this folder")
    ap.add_argument("--model", default=VISION_MODEL,
                    help=f"Gemini model (default: {VISION_MODEL}; pipeline default: {DEFAULT_MODEL})")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="also write full JSON per file to DIR/<name>.json")
    args = ap.parse_args()

    if args.all or not args.files:
        paths = discover_files()
    else:
        paths = [Path(f) for f in args.files]

    paths = [p for p in paths if p.exists()]
    if not paths:
        print("no files to process", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to Vertex (model={args.model})…", file=sys.stderr)
    client = get_client()

    summary = []
    for path in paths:
        print(f"\n→ extracting {path.name} …", file=sys.stderr)
        try:
            doc = extract_vision(path, client, args.model)
        except Exception as e:  # one bad file shouldn't abort the batch
            print(f"  ✗ FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            summary.append((path.name, "ERROR"))
            continue
        display(path, doc)
        if out_dir:
            parent = path.parent.name
            dest = out_dir / (f"{parent} - {path.stem}.json")
            dest.write_text(json.dumps(doc, indent=2, default=str))
            print(f"  → wrote {dest}", file=sys.stderr)
        summary.append((path.name, "review" if doc.get("_needs_review") else "ok"))

    print("\n" + "=" * 78, file=sys.stderr)
    print("SUMMARY:", file=sys.stderr)
    for name, status in summary:
        print(f"  [{status:>6}] {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
