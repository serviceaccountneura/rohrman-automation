#!/usr/bin/env python3
"""
Handwritten-invoice OCR with Gemini 2.5 Flash on Vertex AI.

Uses a Vertex AI service-account JSON for auth, sends each page image to
Gemini 2.5 Flash, and asks for a plain-text transcription that preserves
reading order and visual layout, and distinguishes handwritten content.

Per input file it writes a single <name>.txt.

Usage:
    python main.py test_data/          # folder -> outputs in results/vertex/
    python main.py invoice.pdf scan.jpg ...   # files -> outputs alongside input

Env (optional):
    VERTEX_CREDENTIALS   path to service-account json (default neura_vertex_ai.json)
    VERTEX_LOCATION      Vertex region (default us-central1)
    GEMINI_MODEL         model id (default gemini-2.5-flash)
"""

import sys
from pathlib import Path

from PIL import Image
from google import genai
from google.genai import types

# Shared Vertex/Gemini plumbing lives in pipeline.py and is reused by the web app.
from pipeline import (
    BASE_DIR,
    IMAGE_EXTS,
    MODEL,
    LOCATION,
    OCR_PROMPT,
    _MEDIA_RES,
    get_client,
    pdf_to_pil_images,
    pil_to_part,
)

# Outputs for files passed via a folder go here; individual files save alongside themselves.
RESULTS_DIR = BASE_DIR / "results" / "vertex"


def ocr_image(image: Image.Image, client: genai.Client) -> str:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[pil_to_part(image), OCR_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=32768,
            media_resolution=_MEDIA_RES,
        ),
    )
    return (resp.text or "").strip()


def process_file(path: Path, out_dir: Path, client: genai.Client):
    if path.suffix.lower() == ".pdf":
        pages = pdf_to_pil_images(path)
    else:
        pages = [Image.open(path).convert("RGB")]

    parts = []
    for i, page_img in enumerate(pages, 1):
        print(f"  page {i}/{len(pages)} ...", flush=True)
        text = ocr_image(page_img, client)
        if len(pages) > 1:
            parts.append(f"===== PAGE {i} =====\n{text}")
        else:
            parts.append(text)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{path.stem}.txt"
    out_path.write_text("\n\n".join(parts), encoding="utf-8")
    print(f"  -> {out_path}")


def collect_files(args: list[str]) -> list[tuple[Path, Path]]:
    """Returns (file_path, output_dir) pairs."""
    valid_exts = IMAGE_EXTS | {".pdf"}
    files = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            files.extend((f, RESULTS_DIR) for f in sorted(p.iterdir()) if f.suffix.lower() in valid_exts)
        elif p.is_file() and p.suffix.lower() in valid_exts:
            files.append((p, p.parent))
        else:
            print(f"[skip] {arg} — not a recognised file or directory")
    return files


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <pdf_or_image_or_folder> ...")
        sys.exit(1)

    files = collect_files(sys.argv[1:])
    if not files:
        print("No valid files found.")
        sys.exit(1)

    print(f"Vertex AI: model={MODEL}  location={LOCATION}")
    client = get_client()
    print(f"Processing {len(files)} file(s).\n")

    for path, out_dir in files:
        print(f"{'='*60}\n{path.name}")
        process_file(path, out_dir, client)
        print()


if __name__ == "__main__":
    main()
