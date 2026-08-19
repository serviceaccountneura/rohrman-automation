"""Vendor resolution — DB mapping first, Tekion fetch + human-review fallback."""
from __future__ import annotations

from sqlmodel import Session, select

from api.models.db import VendorMapping
from api.services.tekion_client import TekionApiClient


# Legal suffixes that appear on an invoice but rarely in the mapping table.
_LEGAL_SUFFIXES = (
    "CO", "CO.", "COMPANY", "INC", "INC.", "INCORPORATED", "LLC", "L.L.C.",
    "LLP", "LTD", "LTD.", "LIMITED", "CORP", "CORP.", "CORPORATION", "PLC",
    "USA", "U.S.A.",
)


def _normalize(name: str) -> str:
    """Normalize a vendor name for matching: uppercase, stripped, collapse spaces."""
    return " ".join(name.upper().split())


def _match_key(name: str) -> str:
    """A comparison key that ignores punctuation and trailing legal suffixes.

    Invoices print the full legal name ("DGO Premium Services Co.") while the
    mapping table holds what a clerk typed ("DGO PREMIUM"). Comparing the raw
    normalized strings misses that, and the run fails as "vendor not mapped"
    even though the vendor is mapped.
    """
    text = _normalize(name).replace("&", " AND ")
    # Drop punctuation entirely — "CARBIZZA." and "CARBIZZA" are the same vendor.
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    words = text.split()
    while words and words[-1] in {s.replace(".", "") for s in _LEGAL_SUFFIXES}:
        words.pop()
    return " ".join(words)


def _find_mapping(
    dealer_id: str, vendor_name: str, session: Session
) -> VendorMapping | None:
    """The mapping for this vendor at this dealership, or None.

    Exact match first. Failing that, compare on `_match_key`, and finally allow
    one name to be a prefix of the other — "DGO PREMIUM" against "DGO PREMIUM
    SERVICES". A looser match is only accepted when exactly one mapping
    qualifies: posting a PO to the wrong vendor's account is worse than asking
    someone to confirm, so an ambiguous name is left for human review.
    """
    normalized = _normalize(vendor_name)

    exact = session.exec(
        select(VendorMapping).where(
            VendorMapping.dealer_id == dealer_id,
            VendorMapping.vendor_name == normalized,
        )
    ).first()
    if exact:
        return exact

    key = _match_key(vendor_name)
    if not key:
        return None

    rows = list(
        session.exec(
            select(VendorMapping).where(VendorMapping.dealer_id == dealer_id)
        )
    )

    same_key = [r for r in rows if _match_key(r.vendor_name) == key]
    if len(same_key) == 1:
        print(f"[VENDOR] {vendor_name!r} matched mapping {same_key[0].vendor_name!r}")
        return same_key[0]
    if same_key:
        return None  # ambiguous — do not guess

    prefixed = [
        r
        for r in rows
        if (mk := _match_key(r.vendor_name))
        and (key.startswith(mk + " ") or mk.startswith(key + " "))
    ]
    if len(prefixed) == 1:
        print(
            f"[VENDOR] {vendor_name!r} matched mapping {prefixed[0].vendor_name!r} "
            f"on a partial name"
        )
        return prefixed[0]

    return None


def resolve_vendor(
    dealer_id: str,
    vendor_name: str,
    session: Session,
    client: TekionApiClient,
) -> dict:
    """Resolve a vendor for PO creation.

    1. Look up (dealer_id, normalized vendor_name) in vendor_mappings.
    2. If found, fetch the full vendor record from Tekion by vendorDisplayId.
    3. If not in mapping, search Tekion by name and return candidates for
       human review.

    Returns:
        {"resolved": True, "vendor": {...}}  — ready for PO creation
        {"resolved": False, "reason": "not_in_mapping",
         "candidates": [...], "vendor_name": ...}  — needs human review
    """
    mapping = _find_mapping(dealer_id, vendor_name, session)

    if mapping:
        vendor = client.get_vendor_by_display_id(mapping.vendor_display_id)
        if vendor:
            return {"resolved": True, "vendor": vendor}
        # Mapping exists but Tekion didn't return the vendor — stale mapping.
        return {
            "resolved": False,
            "reason": "stale_mapping",
            "vendor_display_id": mapping.vendor_display_id,
            "vendor_name": vendor_name,
        }

    # Not in mapping — search Tekion for candidates.
    candidates = client.search_vendor(vendor_name)
    if not candidates:
        short = " ".join(vendor_name.split()[:2])
        if short != vendor_name:
            candidates = client.search_vendor(short)

    return {
        "resolved": False,
        "reason": "not_in_mapping",
        "vendor_name": vendor_name,
        "dealer_id": dealer_id,
        "candidates": candidates,
    }
