
import io
import json
import os
from pathlib import Path

import fitz  # pymupdf
from PIL import Image
from google import genai
from google.genai import types
from google.oauth2 import service_account

from doc_schema import build_structure_prompt, default_extraction

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = Path(os.getenv("VERTEX_CREDENTIALS", BASE_DIR / "neura_vertex_ai.json"))
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Higher DPI helps faint handwriting; override with PDF_DPI=<n>.
PDF_DPI = int(os.getenv("PDF_DPI", "400"))

# Give small print/digits more vision tokens. Disable with MEDIA_RES=none.
_MEDIA_RES = (
    None if os.getenv("MEDIA_RES", "high").lower() == "none"
    else types.MediaResolution.MEDIA_RESOLUTION_HIGH
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# --------------------------------------------------------------------------- #
# Stage 1 — OCR prompt: image -> faithful plain text (reading order + handwriting).
# --------------------------------------------------------------------------- #
OCR_PROMPT = """You are a meticulous OCR engine for financial documents — invoices, receipts,
purchase orders, parts statements, ledgers and journal entries — containing PRINTED and
HANDWRITTEN text, often from imperfect scans.

Transcribe everything visible into faithful plain text. Output ONLY the transcription —
no commentary, JSON, or markdown fences.

RULE 1 — VISUAL GROUNDING (overrides everything below):
- Transcribe ONLY what is physically on the page. Never invent, complete, normalise,
  reformat, or recompute anything.
- NEVER alter a digit, amount, or total to make figures "add up." Arithmetic is not your
  job — copy every number exactly as written, even if it looks wrong or inconsistent.
- Use context ONLY to decide between genuinely ambiguous character shapes — never to
  override a character that is clearly written.

RULE 2 — NEVER GUESS:
- Cannot read a token at all -> write [illegible].
- Can partly read / unsure of a token -> best reading then "(?)", e.g. Naylor(?).
  Give ONE reading, not a list of alternates.
- Blank field or empty cell -> leave it blank. Do not fill it in.

READING ORDER & LAYOUT:
- Natural reading order: top-to-bottom, left-to-right, following columns and sections.
- Preserve line breaks and the grouping of related fields.
- Render EVERY table as pipe-delimited rows, one row per line, including the header row:
    | Qty | Description | Unit Price | Amount |
  One cell per column; show an empty cell as a single space between pipes. Do not merge,
  split, or re-order columns.

HANDWRITING:
- Read messy, cursive, faint, or overlapping handwriting stroke by stroke — do not skip it.
- Common confusions to resolve only when truly ambiguous: 0/O, 1/7/l/I, 2/Z, 5/S, 6/G, 8/B,
  rr/n, u/v, cursive a/o/e.
- Preserve numbers exactly: currency symbols, thousands separators, decimals, %, negatives,
  and units ($1,250.00, -376.00, 12.5%).
- Append " (handwritten)" immediately after each handwritten value so it is distinguishable
  from printed text.

ALSO CAPTURE:
- Letterhead/headers (as text), addresses, dates, invoice/PO/account/VIN numbers, line items,
  subtotals, tax, totals, stamps, margin notes, initials, and handwritten corrections.
- Checkboxes as [x] or [ ]; signatures as [signature] plus any legible name.
- Struck-through text: still transcribe it, wrapped as ~~like this~~.
- Ignore bleed-through from the reverse side of the page; transcribe only the front.
"""

def get_client() -> genai.Client:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials not found: {CREDENTIALS_PATH}")
    with open(CREDENTIALS_PATH) as f:
        project = json.load(f)["project_id"]
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return genai.Client(vertexai=True, project=project, location=LOCATION, credentials=creds)


def pdf_to_pil_images(pdf_path: Path) -> list[Image.Image]:
    doc = fitz.open(str(pdf_path))
    mat = fitz.Matrix(PDF_DPI / 72, PDF_DPI / 72)
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
    doc.close()
    return images


def load_pages(path: Path) -> list[Image.Image]:
    """Return one PIL image per page (PDF) or a single image."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return pdf_to_pil_images(path)
    return [Image.open(path).convert("RGB")]


def pil_to_part(image: Image.Image) -> types.Part:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")


# --------------------------------------------------------------------------- #
# Validation — best-effort, since the schema shape is decided by the model.
# Recursively searches the JSON for debit/credit and for arrays of {amount} rows.
# --------------------------------------------------------------------------- #
def _find_number(node, *name_parts):
    """DFS for a numeric value whose key contains any of name_parts."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) \
               and any(p in str(k).lower() for p in name_parts):
                return float(v)
        for v in node.values():
            r = _find_number(v, *name_parts)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_number(v, *name_parts)
            if r is not None:
                return r
    return None


def _amount_arrays(node):
    """Collect lists of objects that carry a numeric 'amount' field (e.g. posting lines)."""
    found = []
    if isinstance(node, dict):
        for v in node.values():
            found += _amount_arrays(v)
    elif isinstance(node, list):
        amts = [it["amount"] for it in node
                if isinstance(it, dict) and isinstance(it.get("amount"), (int, float))
                and not isinstance(it.get("amount"), bool)]
        if amts:
            found.append([float(a) for a in amts])
        for it in node:
            found += _amount_arrays(it)
    return found


def compute_validation(doc: dict) -> dict:
    """Best-effort balance checks over an arbitrary extracted structure."""
    checks, failed = {}, []

    debit = _find_number(doc, "debit")
    credit = _find_number(doc, "credit")
    if debit is not None and credit is not None:
        ok = abs(debit - credit) < 0.01
        checks["debit_equals_credit"] = ok
        if not ok:
            failed.append("debit≠credit")

    arrays = _amount_arrays(doc)
    if arrays:
        biggest = max(arrays, key=len)
        ok = abs(sum(biggest)) < 0.01
        checks["postings_net_zero"] = ok
        if not ok:
            failed.append("postings not zero")

    return {
        "checks": checks,
        "needs_review": bool(failed),
        "notes": ("Validation issues: " + ", ".join(failed)) if failed else None,
    }


def ocr_pages(images: list[Image.Image], client: genai.Client) -> str:
    """Stage 1: OCR each page image to faithful plain text."""
    out = []
    multi = len(images) > 1
    for i, img in enumerate(images, 1):
        resp = client.models.generate_content(
            model=MODEL,
            contents=[pil_to_part(img), OCR_PROMPT],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=32768,
                media_resolution=_MEDIA_RES,
            ),
        )
        text = (resp.text or "").strip()
        out.append(f"===== PAGE {i} =====\n{text}" if multi else text)
    return "\n\n".join(out)


def structure_from_text(ocr_text: str, client: genai.Client) -> dict:
    """Stage 2: ask the model to produce the best-fit JSON structure for this document."""
    resp = client.models.generate_content(
        model=MODEL,
        contents=[build_structure_prompt(ocr_text)],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            max_output_tokens=32768,
        ),
    )
    try:
        data = json.loads(resp.text or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else default_extraction()


def extract_document(images: list[Image.Image], client: genai.Client, progress=None) -> dict:
    """Two-stage extraction: OCR the page images, then structure the text into the schema dict.

    `progress` is an optional callback called with "ocr" then "extract" so the
    web UI can show a staged progress bar.
    """
    if progress:
        progress("ocr")
    text = ocr_pages(images, client)

    if progress:
        progress("extract")
    doc = structure_from_text(text, client)
    # Sidecar keys (underscored) hold our additions, kept out of the model's own JSON.
    doc["_raw_text"] = text
    doc["_validation"] = compute_validation(doc)
    doc["_needs_review"] = doc["_validation"]["needs_review"]
    return doc


def extract_path(path: Path, client: genai.Client, progress=None) -> dict:
    return extract_document(load_pages(path), client, progress=progress)
