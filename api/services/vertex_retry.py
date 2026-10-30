"""Retry Vertex / Gemini calls that fail with quota / rate-limit (HTTP 429).

WHY
    A multi-page batch split and OCR each make one (or a few) generate_content
    calls. When Vertex returns 429 RESOURCE_EXHAUSTED, the splitter used to raise
    SegmentationFailed immediately and the pipeline parked the upload as
    BATCH_SPLIT_FAILED — not retryable — so a transient quota blip permanently
    blocked the scan (Schaumburg Kia 6163316d-… on a 6-page OEM PDF).

    OCR already retried a few times with short sleeps; those waits are too short
    for quota cooldown, and the splitter had no retries at all.

WHAT
    Detect rate-limit errors from the google.genai / API surface (status, code,
    or message) and retry with longer exponential backoff before giving up.
    Callers still raise after the last attempt so permanent failures stay loud.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

# Longer than OCR's old 5/10/15s sleeps — Vertex quota often needs a minute+.
_DEFAULT_ATTEMPTS = 5
_DEFAULT_BASE_SECONDS = 20.0
_DEFAULT_MAX_SECONDS = 180.0


def is_rate_limited(exc: BaseException) -> bool:
    """True when the error is a Vertex/Gemini quota or rate-limit (retryable)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    # google.api_core sometimes nests the code on .error or details.
    nested = getattr(exc, "error", None)
    if nested is not None:
        nested_code = getattr(nested, "code", None) or getattr(nested, "status_code", None)
        if nested_code == 429:
            return True
    text = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "429",
        "resource_exhausted",
        "resource exhausted",
        "rate limit",
        "rate_limit",
        "quota exceeded",
        "quota_exceeded",
        "too many requests",
        "exceeded your current quota",
    )
    return any(n in text for n in needles)


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str = "Vertex",
    attempts: int = _DEFAULT_ATTEMPTS,
    base_seconds: float = _DEFAULT_BASE_SECONDS,
    max_seconds: float = _DEFAULT_MAX_SECONDS,
) -> T:
    """Run ``fn``; on rate-limit errors, sleep and retry. Other errors raise now."""
    last: BaseException | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — surface varies by google client
            last = exc
            if not is_rate_limited(exc) or attempt >= attempts:
                raise
            # Exponential backoff with a little jitter so parallel workers do not
            # all stampede the same second after a shared 429.
            delay = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
            delay = delay * (0.8 + 0.4 * random.random())
            print(
                f"[{what}] rate limited (attempt {attempt}/{attempts}): {exc}; "
                f"retrying in {delay:.0f}s"
            )
            time.sleep(delay)
    assert last is not None
    raise last
