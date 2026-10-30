
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

# Load a .env sitting next to this file (if python-dotenv is installed) so
# VERTEX_CREDENTIALS / VERTEX_LOCATION / GEMINI_MODEL can live in .env.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass  # .env loading is optional; OS env vars still work

CREDENTIALS_PATH = Path(os.getenv("VERTEX_CREDENTIALS", BASE_DIR / "neura_vertex_ai.json"))
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PDF_DPI = 300  # high DPI helps handwriting recognition

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# --------------------------------------------------------------------------- #
# Stage 1 — OCR prompt: image -> faithful plain text (reading order + handwriting).
# --------------------------------------------------------------------------- #
OCR_PROMPT = """You are an expert OCR and document-understanding engine specialised in reading DIFFICULT HANDWRITING on invoices, receipts, purchase orders, parts statements and ledgers.

Transcribe the page image into PLAIN TEXT. Output ONLY the transcribed text — no JSON, no markdown fences, no commentary, no explanations.

LAYOUT & READING ORDER:
- Read the page in natural human reading order (top-to-bottom, left-to-right, following columns and sections) and output the text in that order.
- Preserve the visual layout: keep line breaks, group related fields together, and lay tables out as aligned columns using spaces/tabs so rows and columns line up.
- Keep empty table cells visible as a blank or dash so column alignment is preserved.

READING HANDWRITING (work carefully):
- Examine handwriting stroke by stroke. Zoom mentally into each character before committing. Messy, cursive, faint, or overlapping writing still must be read, not skipped.
- Use surrounding context to resolve ambiguous characters: a value in a "Qty" column is a number; a date field follows a date pattern; a column of figures should arithmetically agree with its total.
- Watch classic confusions and decide from context: 0/O, 1/7/l/I, 2/Z, 5/S, 6/G, 8/B, rr/n, u/v, cursive a/o/e. For digits, prefer the reading that keeps numbers/totals internally consistent.
- Handwritten numbers: preserve thousands separators and decimals exactly as written ($1,250.00 not 1250). Read currency symbols, %, and units faithfully.

TRANSCRIBE EXACTLY:
- Transcribe exactly what is written. Do NOT correct spelling, expand abbreviations, normalise, reformat, or invent values.
- Capture BOTH printed and handwritten content. When a value or line is handwritten, append " (handwritten)" right after it so it is distinguishable from printed text.
- Capture everything: headers, logos/letterhead (as text), addresses, dates, invoice/PO/account numbers, line items, totals, subtotals, tax, stamps, signatures (note as "[signature]" plus any legible name), checkboxes (mark [x] or [ ]), margin notes, initials, and handwritten annotations/corrections.

WHEN UNCERTAIN:
- Make your best context-grounded reading rather than giving up — but never fabricate. If a character/word is genuinely unreadable, transcribe the readable part and mark the rest "[illegible]".
- If a token is readable but you are unsure between options, give your best reading then alternates in brackets, e.g. "Naylor [?Naylar]".
"""

def get_client(location: str | None = None) -> genai.Client:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials not found: {CREDENTIALS_PATH}")
    with open(CREDENTIALS_PATH) as f:
        project = json.load(f)["project_id"]
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return genai.Client(vertexai=True, project=project, location=location or LOCATION, credentials=creds)


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


def _find_number_priority(node, names):
    """First numeric value whose key contains one of `names`, names tried in order."""
    for nm in names:
        v = _find_number(node, nm)
        if v is not None:
            return v
    return None


def _line_items_sum(node) -> float | None:
    """Sum the per-row total of the largest array of line-item-like row dicts."""
    best_sum, best_len = None, 0
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            stack.extend(n.values())
        elif isinstance(n, list):
            if n and all(isinstance(x, dict) for x in n):
                total, cnt = 0.0, 0
                for row in n:
                    for key in ("total_price", "line_total", "extended", "amount", "total"):
                        v = row.get(key)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            total += float(v); cnt += 1; break
                if cnt and len(n) > best_len:
                    best_sum, best_len = total, len(n)
            stack.extend(n)
    return best_sum


def compute_validation(doc: dict) -> dict:
    """Best-effort, DOCUMENT-TYPE-AWARE sanity checks.

    Ledgers (journal/posting) must balance (debit==credit, postings net to 0);
    invoices/POs should have line items that add up to the stated total. Running
    the ledger checks on an invoice falsely flags everything, so we branch on type.
    """
    checks, failed = {}, []
    dt = str(doc.get("document_type") or "").upper()
    is_ledger = any(t in dt for t in ("JOURNAL", "POSTING", "LEDGER"))

    if is_ledger:
        debit = _find_number(doc, "debit")
        credit = _find_number(doc, "credit")
        if debit is not None and credit is not None:
            ok = abs(debit - credit) < 0.01
            checks["debit_equals_credit"] = ok
            if not ok:
                failed.append("debit≠credit")
        arrays = _amount_arrays(doc)
        if arrays:
            ok = abs(sum(max(arrays, key=len))) < 0.01
            checks["postings_net_zero"] = ok
            if not ok:
                failed.append("postings not zero")
    else:
        # Invoice / PO: line items should add up to the stated total (1% tolerance,
        # min 2¢, to allow rounding/tax-line differences without crying wolf).
        items = _line_items_sum(doc)
        stated = _find_number_priority(doc, ["grand_total", "total_amount", "amount_due", "total"])
        if items is not None and stated is not None:
            ok = abs(items - stated) <= max(0.02, abs(stated) * 0.01)
            checks["line_items_match_total"] = ok
            if not ok:
                failed.append(f"line items ({items:.2f}) != total ({stated:.2f})")

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
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=32768),
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
