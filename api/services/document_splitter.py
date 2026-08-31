"""Split a batch scan into one document per invoice.

WHY
    OCR's schema describes a SINGLE document: one vendor, one dealership, one
    flat `identifiers[]`, one `totals[]`. Feed it a stack of three invoices
    scanned into one PDF and there is nowhere to put three of anything, so the
    model flattens them into one record. Downstream then picks one invoice
    number and one total — possibly from different invoices — and posts a
    plausible-looking wrong invoice to Tekion. Nothing errors.

    So a batch scan has to be broken into real separate documents *before* OCR
    runs, not reconciled afterwards.

HOW
    A cheap segmentation pass reads the pages and reports where each invoice
    starts and ends. Anything with more than one invoice is split with PyMuPDF
    into genuine separate PDFs, and each becomes its own row in `documents`.

    Only the boundaries are asked for here, not the content — the full
    extraction still runs per document afterwards. Pages are rendered at a
    lower DPI than extraction uses, since finding "Invoice Number" on a page
    does not need the resolution that reading handwriting does.

THE CASE THAT MUST NOT BREAK
    A multi-page invoice usually repeats its header on every page. The Valvoline
    invoice that prompted this reprints "Invoice Number 135631266 / Invoice Date
    / Due Date" at the top of page 2, so any rule along the lines of "a header
    means a new document" splits one invoice into two and posts both halves.

    Segments are therefore keyed on the invoice NUMBER and merged when it
    repeats. Two pages saying 135631266 are one invoice, however many headers
    they carry.

SAFETY
    Every uncertain outcome falls back to "this is one document", which is the
    behaviour that exists today. A segmentation that fails, returns nothing,
    disagrees about the page count, or produces overlapping or non-contiguous
    ranges is discarded rather than guessed at. Splitting wrongly creates false
    financial records; declining to split just leaves today's behaviour intact.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

import fitz  # noqa: E402
from google.genai import types  # noqa: E402
from PIL import Image  # noqa: E402

# IMPORT ORDER MATTERS — do not let a formatter sort these.
#
# pipeline.py resolves CREDENTIALS_PATH from $VERTEX_CREDENTIALS at import time,
# and ocr_service is what sets that variable. Importing pipeline first freezes
# the wrong path and every call fails with "Credentials not found".
#
# Reusing the model and endpoint from ocr_service also keeps one place to
# upgrade when the Gemini version moves.
from api.services.ocr_service import VISION_LOCATION, VISION_MODEL  # noqa: E402
from pipeline import get_client  # noqa: E402

# Finding a header needs far less resolution than reading handwriting does.
SEGMENT_DPI = 110

SEGMENT_PROMPT = """You are separating a SCANNED BATCH of dealership paperwork into individual
documents. The pages given to you may be one document, or several unrelated documents scanned
together back to back.

Report where each document starts and ends. Do NOT extract the contents.

RULES — read these carefully, getting this wrong is costly:
- A single invoice OFTEN SPANS SEVERAL PAGES and usually REPEATS ITS HEADER (invoice number,
  date, vendor logo) at the top of every page. A repeated header is NOT a new document.
- The invoice number is what identifies a document. Pages sharing the same invoice number are
  ONE document, no matter how many headers or totals appear on them.
- Continuation pages that carry no invoice number (a bare table, "Page 2 of 3", remittance
  instructions, terms and conditions) belong to the document that PRECEDES them.
- A new document begins only when you can see a DIFFERENT invoice number, or an unmistakably
  different vendor.
- Page ranges must be contiguous and must cover every page exactly once, in order. The first
  document starts at page 1 and the last ends at the final page.
- If you are unsure whether something is a separate document, treat it as part of the previous
  one. Reporting one document is always safer than reporting two.

Pages are numbered from 1 in the order given."""


def _segment_schema() -> types.Schema:
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "documents": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "page_start": types.Schema(type=types.Type.INTEGER),
                        "page_end": types.Schema(type=types.Type.INTEGER),
                        "invoice_number": types.Schema(type=types.Type.STRING),
                        "vendor_name": types.Schema(type=types.Type.STRING),
                    },
                    required=["page_start", "page_end"],
                ),
            )
        },
        required=["documents"],
    )


@dataclass
class Segment:
    """One document within a batch scan. Page numbers are 1-based and inclusive."""

    page_start: int
    page_end: int
    invoice_number: str = ""
    vendor_name: str = ""

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1

    def __str__(self) -> str:
        pages = (
            f"page {self.page_start}"
            if self.page_count == 1
            else f"pages {self.page_start}-{self.page_end}"
        )
        return f"{pages}: {self.invoice_number or '(no invoice number)'}"


def page_count(file_path: str | Path) -> int:
    """Pages in the file. Anything that is not a readable PDF counts as one."""
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        with fitz.open(str(path)) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return 1


def _render_for_segmentation(path: Path) -> list[types.Part]:
    """One low-resolution image part per page."""
    parts: list[types.Part] = []
    mat = fitz.Matrix(SEGMENT_DPI / 72, SEGMENT_DPI / 72)
    with fitz.open(str(path)) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            buf = io.BytesIO()
            Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB").save(buf, format="PNG")
            parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
    return parts


def _merge_repeats(segments: list[Segment]) -> list[Segment]:
    """Fold consecutive segments that share an invoice number into one.

    The backstop for a repeated header being read as a new document. Segments
    without an invoice number are left alone — there is nothing to match on, and
    the coverage check below decides whether the result is trustworthy.
    """
    merged: list[Segment] = []
    for seg in segments:
        prev = merged[-1] if merged else None
        key = seg.invoice_number.strip().lower()
        if prev and key and prev.invoice_number.strip().lower() == key:
            prev.page_end = max(prev.page_end, seg.page_end)
            continue
        merged.append(seg)
    return merged


def _covers_exactly(segments: list[Segment], total_pages: int) -> bool:
    """True when the segments tile pages 1..total_pages with no gap or overlap."""
    if not segments:
        return False
    if segments[0].page_start != 1 or segments[-1].page_end != total_pages:
        return False
    for i, seg in enumerate(segments):
        if seg.page_start > seg.page_end:
            return False
        if i and seg.page_start != segments[i - 1].page_end + 1:
            return False
    return True


def segment_documents(file_path: str | Path) -> list[Segment]:
    """The separate documents inside a file.

    Returns one segment for an ordinary single invoice, several for a batch
    scan, and an empty list when segmentation could not be trusted — callers
    treat empty as "process this as one document", the existing behaviour.
    """
    path = Path(file_path)
    total = page_count(path)
    if total < 2:
        return [Segment(1, 1)]

    try:
        parts = _render_for_segmentation(path)
        parts.append(types.Part.from_text(text=SEGMENT_PROMPT))
        client = get_client(location=VISION_LOCATION)
        resp = client.models.generate_content(
            model=VISION_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_segment_schema(),
                max_output_tokens=4096,
            ),
        )
        import json

        raw = json.loads(resp.text or "{}").get("documents") or []
    except Exception as e:  # noqa: BLE001
        print(f"[SPLIT] segmentation failed ({e}); treating as one document")
        return []

    segments: list[Segment] = []
    for entry in raw:
        try:
            segments.append(
                Segment(
                    page_start=int(entry.get("page_start")),
                    page_end=int(entry.get("page_end")),
                    invoice_number=str(entry.get("invoice_number") or "").strip(),
                    vendor_name=str(entry.get("vendor_name") or "").strip(),
                )
            )
        except (TypeError, ValueError):
            print(f"[SPLIT] unusable segment {entry!r}; treating as one document")
            return []

    segments.sort(key=lambda s: s.page_start)
    segments = _merge_repeats(segments)

    if not _covers_exactly(segments, total):
        print(
            f"[SPLIT] segments do not tile {total} pages "
            f"({[str(s) for s in segments]}); treating as one document"
        )
        return []

    # Distinct invoice numbers are the evidence that this really is a batch.
    # Several segments that cannot name themselves are not enough to act on.
    numbers = {s.invoice_number.strip().lower() for s in segments if s.invoice_number.strip()}
    if len(segments) > 1 and len(numbers) < 2:
        print(
            f"[SPLIT] {len(segments)} segments but {len(numbers)} invoice number(s); "
            "treating as one document"
        )
        return [Segment(1, total)]

    return segments


def split_pdf(file_path: str | Path, segments: list[Segment]) -> list[tuple[Segment, str, str]]:
    """Write each segment to its own PDF.

    Returns (segment, path, sha256) per document. The hash is of the split file,
    never the batch: sharing the parent's hash would make every sibling look
    like a duplicate of the others and the whole batch would be held.
    """
    written: list[tuple[Segment, str, str]] = []
    src = fitz.open(str(file_path))
    try:
        for seg in segments:
            out = fitz.open()
            # fitz is 0-based; segments are 1-based and inclusive.
            out.insert_pdf(src, from_page=seg.page_start - 1, to_page=seg.page_end - 1)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.close()
            out.save(tmp.name)
            out.close()
            digest = hashlib.sha256(Path(tmp.name).read_bytes()).hexdigest()
            written.append((seg, tmp.name, digest))
    except Exception:
        for _, p, _ in written:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    finally:
        src.close()
    return written


def child_file_name(parent_name: str, seg: Segment, index: int) -> str:
    """A name that says where this document came from within the batch."""
    stem = Path(parent_name or "batch").stem
    label = seg.invoice_number.strip() or f"part{index}"
    return f"{stem} [{label}].pdf"
