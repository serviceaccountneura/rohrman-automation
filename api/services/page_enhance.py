"""Clean up a page image before Gemini reads it.

The production steps of the lead's preprocess_to_vlm_context.py, applied to
the copy Gemini sees and nothing else -- the stored file, the preview and the
copy attached in Tekion stay the original scan:

    render at 350 DPI  (ocr_service, for PDFs)
    -> crop outer whitespace
    -> upscale x2
    -> grayscale
    -> background normalisation
    -> CLAHE local contrast

Why: the Honda on Grand invoice prints its number so high that the first
digits run into the shaded header bar. Read as scanned, the number came back
as 551937 or 531937 as often as 331937. The image this produces read 331937
on 6 of 6 runs. Background normalisation flattens the shaded bar; CLAHE
brings the strokes back up.

The script's text-detection masks were debug outputs there and are not used
here. Settings are the script's own.

Turned off with OCR_ENHANCE=false, which sends Gemini the page as before.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

# Render resolution for PDFs, from the script.
DPI = 350

CROP_MARGIN = 30
# Pixels lighter than this count as page white when finding the margins.
CONTENT_THRESHOLD = 245
UPSCALE_FACTOR = 2.0
BACKGROUND_SIGMA = 25
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_GRID_SIZE = (12, 12)


# Google refuses a request much over 20 MB, and the pages travel base64-encoded
# (a third larger). Past this many PNG bytes for one document, the pages are
# cleaned up without the x2 upscale instead -- about a quarter of the size.
# Normalisation and CLAHE, the steps that matter, still apply.
MAX_TOTAL_BYTES = 12_000_000


def enhance_pages(images: list[Image.Image]) -> list[Image.Image]:
    """Every page enhanced, at x2 unless that would make the request too big."""
    import io

    def total(pages: list[Image.Image]) -> int:
        size = 0
        for page in pages:
            buf = io.BytesIO()
            page.save(buf, format="PNG")
            size += buf.tell()
        return size

    enlarged = [enhance(img) for img in images]
    if len(enlarged) == 1 or total(enlarged) <= MAX_TOTAL_BYTES:
        return enlarged
    print(f"[OCR] {len(images)} pages too large to send enlarged; enhancing without the x2 upscale")
    plain = [enhance(img, upscale=1.0) for img in images]
    size = total(plain)
    if size <= MAX_TOTAL_BYTES:
        return plain
    # Still too big -- a long unsplit document. Shrink every page until the
    # request fits rather than send one Google will refuse outright. PNG size
    # does not fall exactly with area, so this repeats until it is under.
    pages, scale = plain, 1.0
    for _ in range(6):
        scale *= (MAX_TOTAL_BYTES / size) ** 0.5 * 0.9
        pages = [
            page.resize((max(1, round(page.width * scale)), max(1, round(page.height * scale))), Image.LANCZOS)
            for page in plain
        ]
        size = total(pages)
        if size <= MAX_TOTAL_BYTES:
            break
    print(f"[OCR] long document: pages shrunk to {scale:.0%}, {size / 1e6:.1f} MB")
    return pages


def enhance(image: Image.Image, upscale: float = UPSCALE_FACTOR) -> Image.Image:
    """The page, cropped, enlarged and contrast-corrected; grayscale."""
    bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)

    # Crop outer whitespace.
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    content = cv2.findNonZero((gray < CONTENT_THRESHOLD).astype(np.uint8))
    if content is not None:
        x, y, w, h = cv2.boundingRect(content)
        x1, y1 = max(0, x - CROP_MARGIN), max(0, y - CROP_MARGIN)
        x2 = min(bgr.shape[1], x + w + CROP_MARGIN)
        y2 = min(bgr.shape[0], y + h + CROP_MARGIN)
        bgr = bgr[y1:y2, x1:x2]

    # Upscale, then grayscale.
    if upscale != 1.0:
        bgr = cv2.resize(bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # Background normalisation: divide out the slowly-varying background --
    # scanner shading, tinted paper, the shaded header bar.
    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=BACKGROUND_SIGMA, sigmaY=BACKGROUND_SIGMA)
    normalized = cv2.divide(gray, background, scale=255)

    # Local contrast.
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID_SIZE)
    return Image.fromarray(clahe.apply(normalized))
