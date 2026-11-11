"""Detect Tekion closed-accounting-month refusals and bump the post date.

When Parts/OEM try to post with an invoice dated in a month Tekion has already
closed, Tekion returns errorCode A132 ("Accounting date … cannot be in closed
month…"). Client guidance (Kia / Geneva Nov 2026): use the 1st of the current
open month (e.g. Nov 1 while we are in November).

Distinct from A137 (user restricted to post at a dealer) — that is not fixed
by changing the accounting date.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_DEALER_TZ = "America/Chicago"

# Stable Tekion markers for closed-period posts (captured on prod Kia/Honda).
_CLOSED_MONTH_CODE = "A132"
_CLOSED_MONTH_PHRASE = "cannot be in closed month"


def is_closed_month_error(exc: BaseException | str) -> bool:
    """Whether this Tekion refusal is a closed accounting month (A132)."""
    text = str(exc or "")
    if not text:
        return False
    if _CLOSED_MONTH_CODE in text and _CLOSED_MONTH_PHRASE in text.lower():
        return True
    # Phrase alone is enough — some wrappers drop the code.
    if _CLOSED_MONTH_PHRASE in text.lower():
        return True
    if re.search(r'"errorCode"\s*:\s*"A132"', text):
        return True
    return False


def first_of_current_month_date_str(*, tz_name: str | None = None) -> str:
    """MM/DD/YYYY for day 1 of the current calendar month in the dealer TZ."""
    tz = ZoneInfo((tz_name or DEFAULT_DEALER_TZ).strip() or DEFAULT_DEALER_TZ)
    now = datetime.now(tz)
    return f"{now.month:02d}/01/{now.year}"


def first_of_current_month_ms(*, tz_name: str | None = None) -> int:
    """Epoch ms for midnight on the 1st of the current month (dealer TZ)."""
    tz = ZoneInfo((tz_name or DEFAULT_DEALER_TZ).strip() or DEFAULT_DEALER_TZ)
    now = datetime.now(tz)
    midnight = datetime(now.year, now.month, 1, tzinfo=tz)
    return int(midnight.timestamp() * 1000)
