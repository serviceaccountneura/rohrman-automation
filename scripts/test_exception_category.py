"""Unit checks for exception categories + near-duplicate invoice numbers.

Run:  uv run python scripts/test_exception_category.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from api.services.exception_category import (
    CATEGORY_DUPLICATE,
    CATEGORY_JE,
    CATEGORY_PO,
    CATEGORY_WAITING,
    category_for,
    invoice_digits,
    invoices_are_near_duplicates,
)


def test_categories() -> None:
    assert category_for("NO_GL_ACCOUNT_WRITTEN") == CATEGORY_PO
    assert category_for("PRE_INVOICE_MISSING") == CATEGORY_WAITING
    assert category_for("PO_NOT_FOUND") == CATEGORY_WAITING
    assert category_for("AMOUNT_MISMATCH") == CATEGORY_PO
    assert category_for("INVOICE_NUMBER_MISMATCH") == CATEGORY_PO
    assert category_for("JE_CONTROL_REQUIRED") == CATEGORY_JE
    assert category_for("DUPLICATE_INVOICE") == CATEGORY_DUPLICATE
    assert category_for("DUPLICATE_DOCUMENT") == CATEGORY_DUPLICATE
    assert category_for(None, status="DUPLICATE") == CATEGORY_DUPLICATE


def test_near_duplicates() -> None:
    assert invoice_digits("BLK123") == "123"
    assert invoice_digits("6101029RI") == "6101029"
    assert invoices_are_near_duplicates("BLK123", "123")
    assert invoices_are_near_duplicates("123", "BLK123")
    assert invoices_are_near_duplicates("6101029RI", "6101029")
    assert invoices_are_near_duplicates("BLK 123", "123")
    assert invoices_are_near_duplicates("ABC", "ABC")
    assert not invoices_are_near_duplicates("BLK123", "124")
    assert not invoices_are_near_duplicates("123", "1230")
    assert not invoices_are_near_duplicates("", "123")


def main() -> None:
    tests = [test_categories, test_near_duplicates]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
