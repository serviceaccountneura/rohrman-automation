"""Invoice date → scheduledTime must use dealer TZ midnight, not UTC.

Run:  uv run python scripts/test_invoice_date_tz.py
"""
from __future__ import annotations

import sys
from datetime import timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from api.services.je_creation import (
    DEFAULT_DEALER_TZ,
    _parse_date,
    invoice_date_to_epoch_ms,
)


def test_chicago_midnight_not_utc() -> None:
    """Oakbrook case: paper 10/14 must not become 10/13 in Central."""
    date_str = "10/14/2026"
    ms = invoice_date_to_epoch_ms(date_str, tz_name="America/Chicago")
    as_chicago = __import__("datetime").datetime.fromtimestamp(
        ms / 1000, tz=ZoneInfo("America/Chicago")
    )
    assert as_chicago.strftime("%m/%d/%Y") == "10/14/2026", as_chicago
    assert as_chicago.hour == 0 and as_chicago.minute == 0

    # Old bug: UTC midnight of 10/14 displays as 10/13 evening in Chicago.
    utc_ms = int(
        __import__("datetime")
        .datetime.strptime(date_str, "%m/%d/%Y")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )
    assert ms != utc_ms
    assert ms == 1791954000000  # 2026-10-14T00:00:00-05:00


def test_default_tz_is_chicago() -> None:
    assert DEFAULT_DEALER_TZ == "America/Chicago"
    d = _parse_date("07/16/2026")
    assert str(d.tzinfo) == "America/Chicago"


def main() -> None:
    tests = [test_chicago_midnight_not_utc, test_default_tz_is_chicago]
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
