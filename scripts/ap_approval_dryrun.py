"""Dry run of the AP approval flow — read-only, stops before Post Transaction.

    uv run python scripts/ap_approval_dryrun.py
    uv run python scripts/ap_approval_dryrun.py --po 34267 --invoice TEST-INV-001

Logs into Tekion with the TEKION_* env vars, walks the AP Creation SOP against
the live API, and prints what it found and what it WOULD write. Nothing is
posted: the final Post Transaction call is not implemented on purpose.

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

from api.services.ap_approval import (  # noqa: E402
    HARDCODED_EXPECTED,
    ExpectedInvoice,
    approve_invoice,
)
from api.services.tekion_client import TekionApiClient  # noqa: E402


def _rule(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AP approval dry run (read-only)")
    ap.add_argument("--po", help="PO number to match", default=HARDCODED_EXPECTED.po_number)
    ap.add_argument(
        "--invoice", help="Invoice number to match", default=HARDCODED_EXPECTED.invoice_number
    )
    ap.add_argument(
        "--amount", type=float, help="Expected dollar amount",
        default=HARDCODED_EXPECTED.invoice_amount,
    )
    ap.add_argument(
        "--reference", help="Reference number -> posting Description",
        default=HARDCODED_EXPECTED.reference_number,
    )
    ap.add_argument("--ro", help="RO number -> refText (SUBLET only)", default=HARDCODED_EXPECTED.ro_number)
    ap.add_argument(
        "--type", dest="po_type", default=HARDCODED_EXPECTED.po_type,
        choices=["SUBLET", "MISCELLANEOUS", "STOCK"],
    )
    ap.add_argument("--dealership", help="Dealership name to switch to", default=None)
    ap.add_argument(
        "--list", action="store_true",
        help="Just list the Pre Invoice queue and exit",
    )
    args = ap.parse_args()

    expected = ExpectedInvoice(
        invoice_number=args.invoice,
        po_number=args.po,
        invoice_amount=args.amount,
        reference_number=args.reference,
        ro_number=args.ro,
        po_type=args.po_type,
    )

    _rule("1. Login")
    client = TekionApiClient()  # no DB session — isolated, session not persisted
    client.login()

    if args.list:
        from api.services.ap_approval import ApApprovalService

        _rule("Pre Invoice queue")
        hits = ApApprovalService(client).search_pre_invoiced()
        for h in hits[:25]:
            amt = h.get("invoiceAmount")
            amt = (amt.get("amount", 0) / 100.0) if isinstance(amt, dict) else amt
            pos = ", ".join(str(p.get("poNum")) for p in (h.get("poDetails") or []))
            print(f"  {str(h.get('id')):26s} inv={str(h.get('invoiceNumber') or '-'):20s} "
                  f"${amt}  PO={pos or '-'}  {h.get('payeeName') or ''}")
        if not hits:
            print("  (queue is empty)")
        return 0

    _rule(f"2-4. Approval SOP for PO {expected.po_number} / invoice {expected.invoice_number}")
    result = approve_invoice(client, expected=expected, dealership_name=args.dealership, dry_run=True)

    _rule("Result")
    print(f"  matched                  : {result.matched}")
    print(f"  posted                   : {result.posted}  (Post Transaction not implemented)")
    print(f"  invoice id               : {result.invoice_id}")
    print(f"  invoice number           : {result.invoice_number}")
    print(f"  PO number / id           : {result.po_number} / {result.po_id}")
    print(f"  universalId              : {result.universal_id}")
    print(f"  vendor                   : {result.vendor_name}")
    print(f"  invoice amount           : {result.invoice_amount}")
    print(f"  PO total                 : {result.po_total}")
    print(f"  due date (ms)            : {result.due_date_ms}")
    print(f"  flagged for Parts Manager: {result.flagged_for_parts_manager}")

    if result.discrepancies:
        print("\n  Discrepancies:")
        for d in result.discrepancies:
            print(f"    [X] {d}")
    elif result.invoice_id:
        print("\n  [OK] PO number, invoice number and dollar amount all match.")
    else:
        # No invoice found at all -- "no discrepancies" must not read as a match.
        print("\n  [!] No matching invoice in the Pre Invoice queue. Nothing was compared.")
        print("      It may already have been approved, or the PO/invoice number is wrong.")
        print("      Run with --list to see what is currently pending.")

    if result.postings:
        print("\n  Postings in Tekion now:")
        for p in result.postings:
            print(f"    {p.get('glAccountId'):12s} {str(p.get('amount')):>10s}  "
                  f"description={p.get('description')!r}  refText={p.get('refText')!r}")

    if result.prepared_postings:
        print("\n  What step 4 WOULD write (Description = reference, refText = RO if sublet):")
        for p in result.prepared_postings:
            print(f"    {p.get('glAccountId'):12s} {str(p.get('amount')):>10s}  "
                  f"description={p.get('description')!r}  refText={p.get('refText')!r}")
        print("\n  Prepared payload:")
        print(json.dumps(result.prepared_postings, indent=2)[:2000])

    for n in result.notes:
        print(f"\n  * {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
