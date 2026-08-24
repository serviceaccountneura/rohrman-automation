"""Dry run of the Vendor Stock Order pre-invoice flow — read-only by default.

    uv run python scripts/vso_dryrun.py --po 34449 --invoice 6076404 --amount 446.22
    uv run python scripts/vso_dryrun.py --po 34449 ... --tax 34.96
    uv run python scripts/vso_dryrun.py --po 34449 ... --post      # actually posts

Logs into Tekion, finds the purchase order the invoice refers to, checks the
amounts line up, and prints what it would post. Nothing is written unless
--post is passed.

Unlike the other flows this one creates no purchase order — the PO already
exists and the invoice is being attached to it. A wrong PO number is a hard
stop, which is why the amount check happens before anything is sent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from api.services.tekion_client import TekionApiClient  # noqa: E402
from api.services.vso_po_creation import (  # noqa: E402
    ExpectedStockInvoice,
    pre_invoice_stock_order,
)


def _rule(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Vendor stock order pre-invoice dry run")
    ap.add_argument("--po", required=True, help="Purchase order number from the invoice")
    ap.add_argument("--invoice", required=True, help="Invoice number")
    ap.add_argument("--amount", required=True, type=float, help="Invoice total, tax included")
    ap.add_argument("--tax", type=float, default=0.0, help="Sales tax on the invoice")
    ap.add_argument("--date", default=None, help="Invoice date MM/DD/YYYY")
    ap.add_argument("--dealership", required=True, help="Dealership the PO belongs to")
    ap.add_argument("--file", default=None, help="Invoice PDF to attach")
    ap.add_argument(
        "--post", action="store_true",
        help="Actually pre-invoice the PO in Tekion",
    )
    args = ap.parse_args()

    expected = ExpectedStockInvoice(
        po_number=args.po,
        invoice_number=args.invoice,
        invoice_amount=args.amount,
        sales_tax=args.tax,
        invoice_date=args.date,
        dealership_name=args.dealership,
        invoice_file_path=args.file,
    )

    _rule("1. Login")
    client = TekionApiClient()  # no DB session — isolated, session not persisted
    client.login()

    _rule(f"2-4. Pre-invoice PO {args.po} with invoice {args.invoice}")
    if args.post:
        print("  !! --post given: the PO will be pre-invoiced in Tekion.\n")
    result = pre_invoice_stock_order(
        client, expected, dealership_name=args.dealership, dry_run=not args.post
    )

    _rule("Result")
    print(f"  amounts match            : {result.matched}")
    print(f"  posted                   : {result.posted}")
    print(f"  dealer                   : {result.dealer_id}")
    print(f"  PO number / id           : {result.po_number} / {result.po_id}")
    print(f"  universalId / type       : {result.universal_id} / {result.po_type}")
    print(f"  PO status                : {result.po_status}")
    print(f"  vendor                   : {result.vendor_name}")
    print(f"  PO total                 : {result.po_total}")
    print(f"  invoice net of tax       : {result.net_amount}")
    print(f"  attachment mediaId       : {result.media_id}")
    print(f"  invoice id               : {result.invoice_id}")

    if result.discrepancies:
        print("\n  Discrepancies:")
        for d in result.discrepancies:
            print(f"    [X] {d}")
    elif result.matched:
        print("\n  [OK] The invoice matches the purchase order it names.")

    for n in result.notes:
        print(f"\n  * {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
