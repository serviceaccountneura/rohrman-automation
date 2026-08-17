"""Dry run of the Journal Entry flow — read-only, stops before Save as Draft.

    uv run python scripts/je_creation_dryrun.py
    uv run python scripts/je_creation_dryrun.py --invoice 6045891767 --amount 147.78
    uv run python scripts/je_creation_dryrun.py --list-gl 2410
    uv run python scripts/je_creation_dryrun.py --save     # actually saves a draft

Logs into Tekion with the TEKION_* env vars, walks the Parts Manufacture Ticket
SOP against the live API, and prints the entry it WOULD create. Nothing is
written unless --save is passed, and even then the flow stops at Save as Draft
(a draft is reversible in the UI; Submit is not implemented).

No database and no FastAPI server are needed — this is deliberately isolated
from the PO flow so it can be wired in later.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from api.services.je_creation import (  # noqa: E402
    HARDCODED_EXPECTED,
    ExpectedJournalEntry,
    create_journal_entry,
)
from api.services.tekion_client import TekionApiClient  # noqa: E402


def _rule(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Journal Entry dry run")
    ap.add_argument(
        "--invoice", help="Invoice number -> Description + Reference",
        default=HARDCODED_EXPECTED.invoice_number,
    )
    ap.add_argument(
        "--date", help="Invoice date MM/DD/YYYY -> Accounting Date",
        default=HARDCODED_EXPECTED.invoice_date,
    )
    ap.add_argument(
        "--amount", type=float, help="Invoice amount",
        default=HARDCODED_EXPECTED.invoice_amount,
    )
    ap.add_argument(
        "--dealership", help="Dealership name",
        default=HARDCODED_EXPECTED.dealership_name,
    )
    ap.add_argument(
        "--journal", help="Journal Number / Name (SOP says 76)",
        default=HARDCODED_EXPECTED.journal_number,
    )
    ap.add_argument(
        "--credit-gl", dest="credit_gl", help="Credit GL account number",
        default=HARDCODED_EXPECTED.credit_gl_number,
    )
    ap.add_argument(
        "--debit-gl", dest="debit_gl", help="Debit GL account number",
        default=HARDCODED_EXPECTED.debit_gl_number,
    )
    ap.add_argument(
        "--list-gl", nargs="?", const="", metavar="FILTER",
        help="List the dealership's GL accounts (optionally filtered) and exit",
    )
    ap.add_argument(
        "--save", action="store_true",
        help="Actually save the draft to Tekion (creates a real DRAFT transaction)",
    )
    args = ap.parse_args()

    expected = ExpectedJournalEntry(
        invoice_number=args.invoice,
        invoice_date=args.date,
        invoice_amount=args.amount,
        dealership_name=args.dealership,
        journal_number=args.journal,
        credit_gl_number=args.credit_gl,
        debit_gl_number=args.debit_gl,
    )

    _rule("1. Login")
    client = TekionApiClient()  # no DB session — isolated, session not persisted
    client.login()

    if args.list_gl is not None:
        from api.services.je_creation import JournalEntryService

        dealer_id = client.find_dealer_by_name(args.dealership)
        if not dealer_id:
            print(f"  Could not match dealership '{args.dealership}'")
            return 1
        client.switch_dealer(dealer_id)

        _rule(f"GL accounts for {args.dealership} (dealer {dealer_id})")
        accounts = JournalEntryService(client).gl_accounts()
        needle = (args.list_gl or "").upper()
        shown = 0
        for acc in accounts:
            hay = f"{acc['account_number']} {acc['account_name']}".upper()
            if needle and needle not in hay:
                continue
            flag = "" if acc.get("active", True) else "  (INACTIVE)"
            print(f"  {acc['account_number']:>8s}  {acc['account_id']:<16s} "
                  f"{acc['account_name']}{flag}")
            shown += 1
        print(f"\n  {shown} of {len(accounts)} account(s) shown")
        return 0

    _rule(f"2-3. Parts Manufacture Ticket SOP — journal {expected.journal_number}, "
          f"invoice {expected.invoice_number}")
    if args.save:
        print("  !! --save given: a real DRAFT transaction will be created.\n")
    result = create_journal_entry(client, expected=expected, dry_run=not args.save)

    _rule("Result")
    print(f"  balanced                 : {result.balanced}")
    print(f"  saved                    : {result.saved}")
    print(f"  transaction id / number  : {result.transaction_id} / {result.transaction_number}")
    print(f"  status                   : {result.status}")
    print(f"  dealer                   : {result.dealer_id}")
    print(f"  journal id               : {result.journal_id}")
    print(f"  description              : {result.description}")
    print(f"  reference                : {result.reference}")
    print(f"  accounting date (ms)     : {result.accounting_date_ms}")
    print(f"  control number (MMYY)    : {result.control_number}  "
          f"(known vendor: {result.control_number_known})")
    print(f"  credit / debit / balance : ${result.credit_total:.2f} / "
          f"${result.debit_total:.2f} / ${result.balance:.2f}")

    if result.resolved_accounts:
        print("\n  GL accounts resolved from Tekion:")
        for role, acc in result.resolved_accounts.items():
            print(f"    {role:6s} {acc['account_number']:>8s} -> {acc['account_id']:<16s} "
                  f"{acc['account_name']}")

    if result.discrepancies:
        print("\n  Discrepancies:")
        for d in result.discrepancies:
            print(f"    [X] {d}")
    elif result.postings:
        print("\n  [OK] Both GL accounts resolved and the entry balances to $0.00.")
    else:
        print("\n  [!] No entry was built. Nothing to review.")

    if result.postings:
        verb = "contains" if result.saved else "WOULD contain"
        print(f"\n  What the journal entry {verb}:")
        print(f"    {'GL ACCOUNT':<18s} {'AMOUNT':>10s}  {'CONTROL':<8s} NAME")
        for p in result.postings:
            print(f"    {str(p['glAccountId']):<18s} {p['amount']:>10.2f}  "
                  f"{str(p.get('refId') or ''):<8s} {p['_glAccountName']}")

    if result.payload:
        print("\n  Payload sent to POST /api/accounting/u/v2/transaction/dealer/"
              f"{result.dealer_id}/draft:")
        print(json.dumps(result.payload, indent=2)[:2000])

    for n in result.notes:
        print(f"\n  * {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
