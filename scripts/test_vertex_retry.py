"""Unit checks for Vertex 429 detection / retry classification.

Run:  uv run python scripts/test_vertex_retry.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from api.services.vertex_retry import is_rate_limited


class _Fake429(Exception):
    status_code = 429


def main() -> None:
    cases = [
        (True, _Fake429("quota")),
        (True, Exception("429 RESOURCE_EXHAUSTED")),
        (True, Exception("Resource exhausted: too many requests")),
        (True, Exception("Exceeded your current quota for the model")),
        (False, Exception("segments do not tile 6 pages")),
        (False, Exception("connection reset by peer")),
        (False, ValueError("unusable segment")),
    ]
    failed = 0
    for expect, exc in cases:
        got = is_rate_limited(exc)
        ok = got == expect
        print(("OK " if ok else "FAIL"), repr(str(exc)[:60]), "->", got)
        if not ok:
            failed += 1
    if failed:
        raise SystemExit(f"{failed} check(s) failed")
    print(f"\nAll {len(cases)} checks passed.")


if __name__ == "__main__":
    main()
