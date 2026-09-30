"""Turn a sideways or upside-down scan upright before Gemini reads it.

Pages go to Gemini exactly as scanned. A Honda on Grand invoice fed through
the scanner sideways -- text running vertically on an upright page -- came
back with its invoice number misread ("SS1957"): small print read at 90
degrees is where the model confuses S with 5 and 9 with 6.

Tesseract's orientation-and-script detection (OSD) says how far the text is
turned. Only its orientation check is used; Gemini still does the reading.

Deliberately conservative. The page is only turned when Tesseract is
confident, and anything unexpected -- Tesseract missing, too little text, a
timeout, output that does not parse -- leaves the page exactly as it was. A
missed rotation costs what it always cost; a wrong one would cost more.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

# Tesseract's own scale. Measured on real uploads: every page that was truly
# turned scored 5.1 or more, while an upright Kia core memo (yellow paper,
# mostly empty) was called upside down at 2.46. 4.0 sits between the two.
MIN_CONFIDENCE = 4.0

# Checked on a copy this size; the page itself is turned at full size. Not
# smaller: on the sideways Honda on Grand invoice, confidence was 5.6-5.8 at
# 2500-3000 px, 1.8 at 2000 (under the bar) and at 1200 it guessed the wrong
# way. Small print needs the pixels.
_CHECK_LONG_SIDE = 2800
_TIMEOUT_S = 30


def detect_rotation(image: Image.Image) -> tuple[int, float] | None:
    """(degrees to turn the page clockwise, confidence), or None if unknown."""
    if shutil.which("tesseract") is None:
        return None
    probe = image.convert("L")
    scale = _CHECK_LONG_SIDE / max(probe.size)
    if scale < 1:
        probe = probe.resize((round(probe.width * scale), round(probe.height * scale)))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.png"
        probe.save(path)
        try:
            out = subprocess.run(
                ["tesseract", str(path), "-", "--psm", "0"],
                capture_output=True, text=True, timeout=_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    text = out.stdout + out.stderr
    rotate = re.search(r"Rotate:\s*(\d+)", text)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", text)
    if not rotate or not confidence:
        return None
    return int(rotate.group(1)) % 360, float(confidence.group(1))


def upright(image: Image.Image, label: str = "") -> Image.Image:
    """The page turned so its text reads normally; unchanged if in doubt."""
    found = detect_rotation(image)
    if found is None:
        return image
    degrees, confidence = found
    if degrees == 0:
        return image
    if confidence < MIN_CONFIDENCE:
        print(f"[OCR] {label} looks turned {degrees} degrees but confidence "
              f"{confidence:.2f} is low; left as scanned")
        return image
    # PIL rotates counter-clockwise; Tesseract's "Rotate" is clockwise.
    turned = image.rotate(-degrees, expand=True)

    # Confirm before trusting it: the turned page must now read as upright.
    # A wrong call does not survive this -- turning an upright page over gives
    # a page Tesseract then says is upside down.
    check = detect_rotation(turned)
    if check is None or check[0] != 0:
        print(f"[OCR] {label} looked turned {degrees} degrees (confidence "
              f"{confidence:.2f}) but did not read upright after turning; left as scanned")
        return image
    print(f"[OCR] {label} turned {degrees} degrees upright (confidence {confidence:.2f})")
    return turned
