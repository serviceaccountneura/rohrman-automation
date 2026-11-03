"""Quick checks: money totals must not pass as invoice numbers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.ocr_helpers import (  # noqa: E402
    _clean_invoice_number,
    get_invoice_number,
    looks_like_money_amount,
)

CASES = [
    ("75.00", True, ""),
    ("$75.00", True, ""),
    ("1,234.56", True, ""),
    ("53.82 CR", True, ""),
    ("-1001.52", True, ""),
    ("TEST-OEM-MUQSIT-001", False, "TEST-OEM-MUQSIT-001"),
    ("80090801610A", False, "80090801610A"),
    ("6101029 RI", False, "6101029 RI"),
    ("41854-39", False, "41854-39"),
    ("75", False, "75"),
]


def main() -> int:
    failed = 0
    for raw, is_money, cleaned in CASES:
        m = looks_like_money_amount(raw)
        c = _clean_invoice_number(raw)
        g = get_invoice_number({"invoice_number": raw})
        if m != is_money or c != cleaned or g != cleaned:
            print(
                f"FAIL {raw!r}: money={m} want={is_money} "
                f"clean={c!r} want={cleaned!r} get={g!r}"
            )
            failed += 1
        else:
            print(f"ok {raw!r}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
