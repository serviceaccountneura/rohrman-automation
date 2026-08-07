"""GL account resolution service.

Implements the dealership-specific GL mapping logic:

1. SUBLET PO       → GL 2460 (billed via RO, always)
2. STOCK/VENDOR PO → LLM classifies category (tires/oil/parts) → dealership-specific GL
3. MISCELLANEOUS PO → master vendor table → LLM classifies department → departmental GL
4. OEM PO          → GL 2410 (or 2440/2430 for bulk oil, varies by dealership)

The LLM (gemini-3.1-flash-lite) only classifies — it never picks GL codes.
The rules tables map the classification to the actual GL account.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlmodel import Session, select

from api.models.db import GlVendorMapping
from api.services.vendor_service import _normalize

# ── Vertex AI setup (reuse the OCR pipeline's client) ─────────────────────────

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

PROJECT_ROOT = MAIN_PIPELINE.parent
if not os.environ.get("VERTEX_CREDENTIALS"):
    creds = PROJECT_ROOT / "neura_vertex_ai.json"
    if creds.exists():
        os.environ["VERTEX_CREDENTIALS"] = str(creds)

from google.genai import types
from pipeline import get_client

GL_CLASSIFIER_MODEL = "gemini-2.5-flash-lite"

# ── Department fallback (all dealerships) ─────────────────────────────────────

DEPARTMENT_GL: dict[str, str] = {
    "NEW_VEHICLES": "7191",
    "USED_VEHICLES": "7192",
    "SERVICE": "7193",
    "PARTS": "7195",
    "GENERAL": "7190",
}

# ── Dealership-specific stock GL mappings ─────────────────────────────────────
# (dealership_name_normalized → {category → gl_account})

_STOCK_GL: dict[str, dict[str, str]] = {
    "SCHAUMBURG KIA": {
        "TIRES": "2410",
        "OIL_FLUIDS": "2440",
        "GENERAL_PARTS": "2410",
    },
    "SCHAUMBURG FORD": {
        "TIRES": "2410",
        "OIL_FLUIDS": "2440",
        "GENERAL_PARTS": "2410",
    },
    "SCHAUMBURG HONDA": {
        "TIRES": "2430",
        "OIL_FLUIDS": "2440",
        "GENERAL_PARTS": "2410",
    },
    "OAKBROOK TOYOTA IN WESTMONT": {
        "TIRES": "2411",
        "OIL_FLUIDS": "2430",
        "GENERAL_PARTS": "2410",
    },
}

# OEM bulk oil GL per dealership (default is 2440, Toyota uses 2430).
_OEM_BULK_OIL_GL: dict[str, str] = {
    "OAKBROOK TOYOTA IN WESTMONT": "2430",
}

# ── LLM prompts ───────────────────────────────────────────────────────────────

_DEPARTMENT_PROMPT = """You are an AP Accounting Classifier for an Automotive Dealership.

DEPARTMENT DEFINITIONS:
- SERVICE: Mechanic tools, socket sets, torque wrenches, shop supplies, floor cleaners, nitrile gloves, chemicals, shop towels, technician equipment.
- PARTS: Physical vehicle replacement inventory (brake pads, filters, spark plugs, tires, OEM components for resale/stock).
- NEW_VEHICLES: Supplies specifically used by the New Car Sales Department.
- USED_VEHICLES: Supplies specifically used for Used Car reconditioning.
- GENERAL: Shared administrative expenses, office supplies, dealership-wide facilities.

Classify these line items into a single department:
{descriptions}

Respond with ONLY one of these exact words: SERVICE, PARTS, GENERAL, NEW_VEHICLES, or USED_VEHICLES."""

_CATEGORY_PROMPT = """You are a dealership parts inventory assistant. Classify the following parts into the inventory category.

Categories:
- TIRES — tires, wheels, rims, tire-related hardware (valve stems, wheel weights, TPMS)
- OIL_FLUIDS — motor oil, transmission fluid, coolant, antifreeze, DEF, grease, lubricants, brake fluid, power steering fluid
- GENERAL_PARTS — everything else (brake pads, filters, belts, hardware, body parts, electrical, etc.)

Line item descriptions:
{descriptions}

Respond with ONLY the category name (one of: TIRES, OIL_FLUIDS, GENERAL_PARTS). No other text."""

_BULK_OIL_PROMPT = """You are a dealership parts assistant. Determine if the following line items are bulk oil/fluid purchases (large quantity drums, totes, or bulk deliveries) or regular packaged parts.

Line item descriptions:
{descriptions}

Respond with ONLY "BULK" or "REGULAR". No other text."""


# ── LLM classification ────────────────────────────────────────────────────────

def _llm_classify(prompt: str, valid_answers: set[str], default: str) -> str:
    """Call Gemini flash-lite and return a classification, falling back to default."""
    try:
        client = get_client()
        resp = client.models.generate_content(
            model=GL_CLASSIFIER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=20,
            ),
        )
        answer = (resp.text or "").strip().upper()
        # Extract just the label — the model might add punctuation or whitespace.
        for valid in valid_answers:
            if valid in answer:
                return valid
        return default
    except Exception as e:
        print(f"[GL] LLM classification failed ({e}), using default: {default}")
        return default


def _detect_department(descriptions: list[str]) -> str:
    """Classify line items into a department using the LLM."""
    desc_text = "\n".join(f"- {d}" for d in descriptions)
    prompt = _DEPARTMENT_PROMPT.format(descriptions=desc_text)
    return _llm_classify(
        prompt,
        valid_answers={"NEW_VEHICLES", "USED_VEHICLES", "SERVICE", "PARTS", "GENERAL"},
        default="GENERAL",
    )


def _detect_category(descriptions: list[str]) -> str:
    """Classify line items into an inventory category using the LLM."""
    desc_text = "\n".join(f"- {d}" for d in descriptions)
    prompt = _CATEGORY_PROMPT.format(descriptions=desc_text)
    return _llm_classify(
        prompt,
        valid_answers={"TIRES", "OIL_FLUIDS", "GENERAL_PARTS"},
        default="GENERAL_PARTS",
    )


def _detect_bulk_oil(descriptions: list[str]) -> bool:
    """Determine if line items are bulk oil/fluid using the LLM."""
    desc_text = "\n".join(f"- {d}" for d in descriptions)
    prompt = _BULK_OIL_PROMPT.format(descriptions=desc_text)
    result = _llm_classify(prompt, valid_answers={"BULK", "REGULAR"}, default="REGULAR")
    return result == "BULK"


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_gl(
    po_type: str,
    dealership_name: str,
    vendor_name: str,
    line_descriptions: list[str],
    session: Session,
) -> str:
    """Resolve the GL account for a PO based on type, dealership, vendor, and line items.

    Args:
        po_type: "SUBLET", "MISCELLANEOUS", "STOCK", or "OEM"
        dealership_name: e.g. "Schaumburg Honda"
        vendor_name: vendor name from the invoice
        line_descriptions: list of line item descriptions from OCR
        session: DB session for vendor GL lookups

    Returns:
        GL account code string, e.g. "7193"
    """
    dealership_norm = _normalize(dealership_name)

    if po_type == "SUBLET":
        return "2460"

    if po_type == "OEM":
        if _detect_bulk_oil(line_descriptions):
            return _OEM_BULK_OIL_GL.get(dealership_norm, "2440")
        return "2410"

    if po_type == "STOCK":
        category = _detect_category(line_descriptions)
        dealer_map = _STOCK_GL.get(dealership_norm)
        if dealer_map:
            return dealer_map.get(category, "2410")
        # Default for unmapped dealerships
        if category == "OIL_FLUIDS":
            return "2440"
        return "2410"

    if po_type == "MISCELLANEOUS":
        return _resolve_misc_gl(vendor_name, line_descriptions, session)

    # Unknown PO type — safe fallback
    return "7190"


def _resolve_misc_gl(
    vendor_name: str,
    line_descriptions: list[str],
    session: Session,
) -> str:
    """Resolve GL for a miscellaneous PO.

    1. Check master vendor-to-GL table (deterministic)
    2. If not found, LLM classifies department → departmental fallback
    """
    # Step 1: Check master vendor table
    vendor_norm = _normalize(vendor_name)
    row = session.exec(
        select(GlVendorMapping).where(GlVendorMapping.vendor_name == vendor_norm)
    ).first()
    if row:
        return row.gl_account

    # Step 2: LLM department classification → departmental fallback
    department = _detect_department(line_descriptions)
    return DEPARTMENT_GL.get(department, "7190")
