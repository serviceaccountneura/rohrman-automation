"""OCR service — wraps the existing Vertex AI vision_extract pipeline.

Calls extract_vision() from main_pipeline/vision_extract.py to get structured
JSON from a PDF/image, then returns it for the frontend to display and edit.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

PROJECT_ROOT = MAIN_PIPELINE.parent
if not os.environ.get("VERTEX_CREDENTIALS"):
    creds = PROJECT_ROOT / "neura_vertex_ai.json"
    if creds.exists():
        os.environ["VERTEX_CREDENTIALS"] = str(creds)

from google.genai import types  # noqa: E402

from api.services.ocr_helpers import to_flat_fields  # noqa: E402
from normalize import to_po_contract  # noqa: E402
from pipeline import get_client, load_pages, pil_to_part  # noqa: E402
from vision_extract import VISION_PROMPT, build_response_schema, validate  # noqa: E402

VISION_MODEL = "gemini-2.5-pro"


def extract_document(file_path: str | Path) -> dict[str, Any]:
    """Run vision-first extraction on a PDF/image file.

    Returns the structured JSON dict (same shape as vision_extract.py produces).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    client = get_client()
    images = load_pages(path)
    parts = [pil_to_part(img) for img in images]
    parts.append(types.Part.from_text(text=VISION_PROMPT))

    resp = client.models.generate_content(
        model=VISION_MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=build_response_schema(),
            max_output_tokens=65535,
        ),
    )

    try:
        doc = json.loads(resp.text or "{}")
    except json.JSONDecodeError:
        doc = {"document_type": None, "_parse_error": (resp.text or "")[:500]}

    if not isinstance(doc, dict):
        doc = {"document_type": None}

    doc["_pages"] = len(images)
    doc["_po_contract"] = to_po_contract(doc)
    doc["_validation"] = validate(doc)
    doc["_needs_review"] = doc["_validation"]["needs_review"]
    # Flat, predictable field names for callers that should not have to dig
    # through identifiers[]/totals[] themselves — notably the frontend.
    doc["_fields"] = to_flat_fields(doc)
    return doc
