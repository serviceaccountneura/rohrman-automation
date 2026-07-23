#!/usr/bin/env python3
"""Split multi-page PDFs into individual pages and correct orientation.

For each PDF in the Sample files directory:
  - If the PDF has more than one page, create a folder named after the PDF
  - Render each page as a separate PDF with corrected orientation
  - Single-page PDFs are left untouched

Uses pymupdf (fitz) for page splitting and orientation detection.
"""
import sys
from pathlib import Path

try:
    import fitz  # pymupdf
except ImportError:
    print("pymupdf not installed. Run: pip install pymupdf")
    sys.exit(1)


SAMPLE_DIR = Path(__file__).parent / "Sample files"


def correct_page_orientation(page: fitz.Page) -> fitz.Page:
    """Detect and correct page rotation.

    pymupdf exposes page.rotation (0, 90, 180, 270).
    We set it to 0 and apply the rotation to the page content so
    downstream renderers (OCR, viewers) see it upright.
    """
    rot = page.rotation
    if rot != 0:
        # Apply the rotation permanently to the page content
        page.set_rotation(0)
        # Rotate the page content to match
        rect = page.rect
        if rot == 90:
            page.set_cropbox(fitz.Rect(0, 0, rect.height, rect.width))
        elif rot == 270:
            page.set_cropbox(fitz.Rect(0, 0, rect.height, rect.width))
    return page


def split_pdf(pdf_path: Path) -> int:
    """Split a PDF into individual pages. Returns number of pages created."""
    doc = fitz.open(str(pdf_path))
    num_pages = len(doc)

    if num_pages <= 1:
        print(f"  {pdf_path.name}: {num_pages} page(s) -- skipping (single page)")
        doc.close()
        return 0

    # Create folder named after the PDF (without extension)
    folder_name = pdf_path.stem
    out_dir = pdf_path.parent / folder_name
    out_dir.mkdir(exist_ok=True)

    print(f"  {pdf_path.name}: {num_pages} pages -> splitting into {out_dir.name}/")

    for i in range(num_pages):
        # Create a new single-page PDF
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=i, to_page=i)

        # Correct orientation on the inserted page
        page = new_doc[0]
        correct_page_orientation(page)

        # Save as page_1.pdf, page_2.pdf, etc.
        out_path = out_dir / f"page_{i + 1}.pdf"
        new_doc.save(str(out_path))
        new_doc.close()
        print(f"    -> {out_path.name}")

    doc.close()
    return num_pages


def main():
    if not SAMPLE_DIR.exists():
        print(f"Directory not found: {SAMPLE_DIR}")
        sys.exit(1)

    pdfs = sorted(SAMPLE_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {SAMPLE_DIR}")
        return

    print(f"Processing {len(pdfs)} PDF(s) in {SAMPLE_DIR}\n")

    total_split = 0
    total_pages = 0
    for pdf in pdfs:
        pages = split_pdf(pdf)
        if pages > 0:
            total_split += 1
            total_pages += pages

    print(f"\nDone. Split {total_split} PDF(s) into {total_pages} individual pages.")


if __name__ == "__main__":
    main()
