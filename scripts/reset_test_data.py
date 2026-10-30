"""Clear processed documents so a test run can start clean.

    uv run python scripts/reset_test_data.py            # documents + notifications
    uv run python scripts/reset_test_data.py --yes      # skip the confirmation
    uv run python scripts/reset_test_data.py --dry-run  # show what would go
    uv run python scripts/reset_test_data.py --all      # also caches and the Tekion session

Deletes only test/run artefacts. Users, vendor mappings and the GL vendor table
are never touched — losing the 387 seeded vendor mappings would mean re-running
migrations to get them back.

IMPORTANT: this clears *our* records, not Tekion's. Purchase orders, pre-invoices
and journal entries already sent to Tekion stay there and must be voided or
deleted in Tekion itself. Re-uploading the same invoice after a reset will post
to Tekion a second time, because the duplicate check reads the table this script
just emptied.
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

from sqlmodel import Session, delete, select  # noqa: E402

from api.db import engine  # noqa: E402
from api.models.db import (  # noqa: E402
    Document,
    Notification,
    TekionGlAccount,
    TekionSession,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Clear test documents")
    ap.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="Only report what would go")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Also clear the cached GL accounts and the saved Tekion session "
        "(forces a fresh Tekion login and GL fetch next run)",
    )
    args = ap.parse_args()

    with Session(engine) as session:
        documents = list(session.exec(select(Document)))
        notifications = list(session.exec(select(Notification)))
        gl_accounts = list(session.exec(select(TekionGlAccount))) if args.all else []
        sessions = list(session.exec(select(TekionSession))) if args.all else []

        print(f"  documents          : {len(documents)}")
        print(f"  notifications      : {len(notifications)}")
        if args.all:
            print(f"  cached GL accounts : {len(gl_accounts)}")
            print(f"  Tekion sessions    : {len(sessions)}")

        posted = [d for d in documents if d.transaction_number or d.po_number]
        if posted:
            print()
            print(f"  {len(posted)} of these reached Tekion and will REMAIN there:")
            for d in posted[:10]:
                ref = d.transaction_number or d.po_number
                print(f"    {ref:>10s}  {d.dealership_name} — {d.file_name[:40]}")
            if len(posted) > 10:
                print(f"    …and {len(posted) - 10} more")

        if args.dry_run:
            print("\nDry run — nothing deleted.")
            return 0

        if not documents and not notifications and not gl_accounts and not sessions:
            print("\nNothing to delete.")
            return 0

        if not args.yes:
            print()
            answer = input("Delete these rows? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return 1

        # Remove the working files the queue kept on disk before dropping the
        # rows that point at them, or they are orphaned in the temp directory.
        removed_files = 0
        for doc in documents:
            if not doc.source_path:
                continue
            try:
                if Path(doc.source_path).unlink(missing_ok=True) is None:
                    removed_files += 1
            except OSError:
                pass

        session.exec(delete(Notification))
        session.exec(delete(Document))
        if args.all:
            session.exec(delete(TekionGlAccount))
            session.exec(delete(TekionSession))
        session.commit()

        print()
        print(f"Deleted {len(documents)} document(s), {len(notifications)} notification(s).")
        if args.all:
            print(
                f"Cleared {len(gl_accounts)} cached GL account(s) and "
                f"{len(sessions)} Tekion session(s)."
            )
        if removed_files:
            print(f"Removed {removed_files} leftover upload file(s).")
        print("Vendor mappings, GL vendor table and users were left alone.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
