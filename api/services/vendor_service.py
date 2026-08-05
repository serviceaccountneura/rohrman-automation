"""Vendor resolution — DB mapping first, Tekion fetch + human-review fallback."""
from __future__ import annotations

from sqlmodel import Session, select

from api.models.db import VendorMapping
from api.services.tekion_client import TekionApiClient


def _normalize(name: str) -> str:
    """Normalize a vendor name for matching: uppercase, stripped, collapse spaces."""
    return " ".join(name.upper().split())


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
    normalized = _normalize(vendor_name)

    mapping = session.exec(
        select(VendorMapping).where(
            VendorMapping.dealer_id == dealer_id,
            VendorMapping.vendor_name == normalized,
        )
    ).first()

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
