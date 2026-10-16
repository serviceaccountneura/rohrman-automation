"""Clean up page images before reading -- two finishes, one geometry.

The lead's preprocess_to_vlm_context.py steps are split here:

* **VLM finish** (``enhance`` / ``enhance_pages``) -- continuous grayscale with
  background normalisation + CLAHE. Tuned so Gemini can read digits that sit
  in shaded header bars (Honda on Grand: 331937 vs 551937/531937).
* **OCR finish** (``enhance_for_ocr``) -- same crop and upscale, then denoise,
  flatten shading, and mild CLAHE so Docling sees clear
  strokes without the hard binarisation that erased handwriting.

Geometry is shared on purpose: 2.5 Pro boxes fields on the VLM pages; crop
OCRs must read the same normalised coordinates on the OCR pages.

The stored file, preview, and Tekion attachment stay the original scan.
Turned off with OCR_ENHANCE=false.
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

# OCR finish stays continuous grayscale (hard page binarisation crushed
# handwritten GL amounts). Printed fields can still be sharpened at crop time.
_OCR_DENOISE_D = 5
_OCR_CLAHE_CLIP = 1.5
_OCR_CLAHE_TILE = (8, 8)


# Google refuses a request much over 20 MB, and the pages travel base64-encoded
# (a third larger). Past this many PNG bytes for one document, the pages are
# cleaned up without the x2 upscale instead -- about a quarter of the size.
# Normalisation and CLAHE, the steps that matter, still apply.
MAX_TOTAL_BYTES = 12_000_000


def _png_bytes(pages: list[Image.Image]) -> int:
    import io

    size = 0
    for page in pages:
        buf = io.BytesIO()
        page.save(buf, format="PNG")
        size += buf.tell()
    return size


def _crop_and_upscale(image: Image.Image, upscale: float) -> np.ndarray:
    """Shared geometry: whitespace crop + optional upscale, as BGR."""
    bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    content = cv2.findNonZero((gray < CONTENT_THRESHOLD).astype(np.uint8))
    if content is not None:
        x, y, w, h = cv2.boundingRect(content)
        x1, y1 = max(0, x - CROP_MARGIN), max(0, y - CROP_MARGIN)
        x2 = min(bgr.shape[1], x + w + CROP_MARGIN)
        y2 = min(bgr.shape[0], y + h + CROP_MARGIN)
        bgr = bgr[y1:y2, x1:x2]

    if upscale != 1.0:
        bgr = cv2.resize(bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    return bgr


def _flatten_background(gray: np.ndarray) -> np.ndarray:
    """Divide out slowly-varying shading (header bars, tinted paper)."""
    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=BACKGROUND_SIGMA, sigmaY=BACKGROUND_SIGMA)
    return cv2.divide(gray, background, scale=255)


def enhance(image: Image.Image, upscale: float = UPSCALE_FACTOR) -> Image.Image:
    """VLM finish: cropped, enlarged, background-flattened, CLAHE grayscale."""
    bgr = _crop_and_upscale(image, upscale)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    normalized = _flatten_background(gray)
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID_SIZE)
    return Image.fromarray(clahe.apply(normalized))


def enhance_for_ocr(image: Image.Image, upscale: float = UPSCALE_FACTOR) -> Image.Image:
    """OCR finish: same geometry as ``enhance``, soft grayscale for engines.

    Denoise + background flatten lift shaded/gray print; mild CLAHE keeps
    handwritten strokes continuous (full-page adaptive binary was destroying
    GL amount crops). Returned as RGB for Docling.
    """
    bgr = _crop_and_upscale(image, upscale)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    denoised = cv2.bilateralFilter(gray, _OCR_DENOISE_D, 50, 50)
    normalized = _flatten_background(denoised)
    clahe = cv2.createCLAHE(clipLimit=_OCR_CLAHE_CLIP, tileGridSize=_OCR_CLAHE_TILE)
    return Image.fromarray(clahe.apply(normalized)).convert("RGB")


def _fit_request_size(
    images: list[Image.Image],
    finish,
) -> list[Image.Image]:
    """Apply ``finish(img, upscale=…)`` and shrink if the VLM upload would be too big."""
    enlarged = [finish(img, upscale=UPSCALE_FACTOR) for img in images]
    if len(enlarged) == 1 or _png_bytes(enlarged) <= MAX_TOTAL_BYTES:
        return enlarged
    print(f"[OCR] {len(images)} pages too large to send enlarged; enhancing without the x2 upscale")
    plain = [finish(img, upscale=1.0) for img in images]
    size = _png_bytes(plain)
    if size <= MAX_TOTAL_BYTES:
        return plain
    pages, scale = plain, 1.0
    for _ in range(6):
        scale *= (MAX_TOTAL_BYTES / size) ** 0.5 * 0.9
        pages = [
            page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.LANCZOS,
            )
            for page in plain
        ]
        size = _png_bytes(pages)
        if size <= MAX_TOTAL_BYTES:
            break
    print(f"[OCR] long document: pages shrunk to {scale:.0%}, {size / 1e6:.1f} MB")
    return pages


def _choose_upscale(images: list[Image.Image]) -> float | None:
    """Upscale factor both finishes should share, or None when a final shrink is needed.

    Tries x2, then x1. Returns None if even x1 VLM pages exceed the upload cap
    (caller must shrink both finishes identically afterward).
    """
    probe = [enhance(img, upscale=UPSCALE_FACTOR) for img in images]
    if len(probe) == 1 or _png_bytes(probe) <= MAX_TOTAL_BYTES:
        return UPSCALE_FACTOR
    print(f"[OCR] {len(images)} pages too large to send enlarged; enhancing without the x2 upscale")
    probe = [enhance(img, upscale=1.0) for img in images]
    if _png_bytes(probe) <= MAX_TOTAL_BYTES:
        return 1.0
    return None


def enhance_pages(images: list[Image.Image]) -> list[Image.Image]:
    """Every page with the VLM finish, at x2 unless that would make the request too big."""
    return _fit_request_size(images, enhance)


def enhance_pages_pair(
    images: list[Image.Image],
) -> tuple[list[Image.Image], list[Image.Image]]:
    """VLM pages and OCR pages with identical crop / scale / any final shrink.

    Pro boxes on the VLM list; Docling crops the OCR list.
    """
    upscale = _choose_upscale(images)
    if upscale is not None:
        return (
            [enhance(img, upscale=upscale) for img in images],
            [enhance_for_ocr(img, upscale=upscale) for img in images],
        )

    # Same shrink path as enhance_pages, applied to both finishes together.
    vlm = [enhance(img, upscale=1.0) for img in images]
    ocr = [enhance_for_ocr(img, upscale=1.0) for img in images]
    size = _png_bytes(vlm)
    scale = 1.0
    for _ in range(6):
        scale *= (MAX_TOTAL_BYTES / size) ** 0.5 * 0.9
        vlm = [
            page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.LANCZOS,
            )
            for page in vlm
        ]
        ocr = [
            page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.LANCZOS,
            )
            for page in ocr
        ]
        size = _png_bytes(vlm)
        if size <= MAX_TOTAL_BYTES:
            break
    print(f"[OCR] long document: pages shrunk to {scale:.0%} (VLM+OCR), {size / 1e6:.1f} MB")
    return vlm, ocr
