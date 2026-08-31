"""Show the journal entry a vehicle manufacturer invoice would produce.

    uv run python scripts/vmi_je_dryrun.py path/to/invoice.pdf
    uv run python scripts/vmi_je_dryrun.py path/to/ocr.json --dealership "Bob Rohrman Schaumburg Kia"

Reads the invoice (or a saved OCR result), builds the entry, prints every
posting line, and stops. Nothing is sent to Tekion.

Use this rather than uploading through the UI while a template is being worked
out: an upload posts a real draft, and a draft that is wrong still has to be
found and deleted by a person.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from api.services import ocr_helpers, vmi_helpers  # noqa: E402
from api.services.je_creation import JournalEntryService  # noqa: E402
from api.services.vmi_je_creation import (  # noqa: E402
    TEMPLATES,
    VehicleJournalEntryService,
)


def _load_ocr(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    from api.services.ocr_service import extract_document

    print(f"Running OCR on {path.name} — this takes 30-60 seconds...")
    return extract_document(str(path))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("invoice", help="PDF/image to read, or a saved OCR .json")
    ap.add_argument("--dealership", default="", help="overrides the dealership OCR read")
    args = ap.parse_args()

    path = Path(args.invoice)
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    ocr = _load_ocr(path)
    facts = vmi_helpers.build_facts(ocr, args.dealership)

    print("\n── What was read off the invoice ──")
    print(f"  manufacturer     {facts.manufacturer or '(not identified)'}")
    print(f"  dealership       {facts.dealership_name or '-'}")
    print(f"  invoice          {facts.invoice_number or '-'}  {facts.invoice_date or '-'}")
    print(f"  stock number     {facts.stock_number or '(none — must be handwritten)'}")
    print(f"  VIN              {facts.vin or '-'}")
    print(f"  dealer cost      {facts.dealer_cost_total:,.2f}")
    print(f"  MSRP             {facts.msrp_total:,.2f}")
    print(f"  handwritten $    {facts.annotated_amounts or '(none found)'}")
    print(f"  handwritten GL   {facts.annotated_gl_accounts or '(none found)'}")

    template = TEMPLATES.get(facts.manufacturer)
    if template is None:
        print(f"\nREFUSED: no template for {facts.manufacturer or '(unknown make)'}")
        return 2
    if template.unspecified_reason:
        print(f"\nREFUSED: {facts.manufacturer} — {template.unspecified_reason}")
        return 2

    missing = [key for key in template.requires if facts.amount(key) is None]
    if missing:
        print(f"\nREFUSED: needs {', '.join(missing)} written on the invoice")
        return 2

    # Fake GL ids: this never talks to Tekion, so the chart is not resolved. The
    # account NUMBERS are what matter for reviewing the shape of the entry.
    resolved = {
        n: {"account_id": f"(unresolved:{n})", "account_name": "", "active": True}
        for n in {line.gl_number for line in template.lines}
    }
    postings = VehicleJournalEntryService.build_postings(
        template, facts, resolved, "DEALER"
    )

    print("\n── Postings ──")
    print(f"  {'GL':>6} {'amount':>14}  control")
    for p in postings:
        print(f"  {p['_glAccountNumber']:>6} {p['amount']:>14,.2f}  {p['_control']}")

    credit, debit, balance = JournalEntryService.check_balance(postings)
    print(f"\n  credit {credit:>12,.2f}")
    print(f"  debit  {debit:>12,.2f}")
    print(f"  balance{balance:>12,.2f}  {'OK' if abs(balance) < 0.005 else 'DOES NOT BALANCE'}")
    return 0 if abs(balance) < 0.005 else 3


if __name__ == "__main__":
    raise SystemExit(main())
