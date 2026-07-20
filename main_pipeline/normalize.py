"""
normalize.py — Map the free-form Gemini extraction (model-decided schema) into a
FIXED "PO contract" that the Tekion fill scripts can consume deterministically.

The stage-2 prompt only *suggests* field names, so we search tolerantly: by key
substring, in priority order, recursively — skipping our underscored sidecar keys.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


def _walk(node: Any) -> Iterable[tuple[str, Any]]:
    """Yield (key, value) for every dict entry, recursively. Skips _sidecar keys."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            yield k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def find_key(doc: Any, names: list[str], want_type: type | tuple = None) -> Any:
    """First value whose key contains one of `names` (case-insensitive), names tried in priority order."""
    for nm in names:
        nl = nm.lower()
        for k, v in _walk(doc):
            if nl in str(k).lower() and (want_type is None or isinstance(v, want_type)):
                if v not in (None, ""):
                    return v
    return None


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^0-9.\-]", "", v)
        if s in ("", "-", ".", "-.", "--"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _vendor(doc: Any) -> dict:
    v = find_key(doc, ["vendor", "supplier"], dict)
    vid = name = None
    if isinstance(v, dict):
        name = v.get("name") or find_key(v, ["name"], str)
        vid = v.get("id") or find_key(v, ["id", "code", "number"], str)
    if not name:
        s = find_key(doc, ["vendor", "supplier"], str)
        if isinstance(s, str):
            m = re.match(r"\s*([0-9][0-9_\-]*)\s+(.*)", s)  # "1711_6028 NAPA AUTO PARTS"
            if m:
                vid, name = m.group(1), m.group(2).strip()
            else:
                name = s.strip()
    return {"id": vid, "name": name}


def _line_items(doc: Any) -> list[dict]:
    """Largest array of part-like row objects."""
    best: list = []
    for _, v in _walk(doc):
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            keys = " ".join(str(kk).lower() for x in v for kk in x.keys())
            if any(t in keys for t in ("part", "qty", "quantity", "price", "amount", "description")):
                if len(v) > len(best):
                    best = v
    items = []
    for row in best:
        items.append({
            "part": find_key(row, ["part_number", "part_no", "part", "description", "item"], str),
            "qty": to_number(find_key(row, ["qty", "quantity", "count"])),
            "unit_price": to_number(find_key(row, ["unit_price", "unit_cost", "unit", "price", "cost"])),
            "total_price": to_number(find_key(row, ["total_price", "extended", "line_total", "total", "amount"])),
        })
    return items


def to_po_contract(doc: dict) -> dict:
    """Free-form Gemini doc -> fixed contract for the Tekion fill scripts."""
    validation = doc.get("_validation") or {}
    return {
        "document_type": doc.get("document_type") or find_key(doc, ["document_type", "doc_type"], str),
        "dealership": find_key(doc, ["dealership", "store", "location"], str),
        "vendor": _vendor(doc),
        "invoice_number": find_key(doc, ["invoice_number", "invoice_no", "invoice number", "invoice"]),
        "po_number": find_key(doc, ["purchase_order_number", "po_number", "po number", "purchase order"]),
        "control_number": find_key(doc, ["control_number", "control_no", "control"]),
        "line_items": _line_items(doc),
        "total": to_number(find_key(doc, ["grand_total", "total_amount", "amount_due", "balance_due", "total"])),
        "needs_review": bool(validation.get("needs_review", False)),
        "validation": validation.get("checks") or {},
    }
