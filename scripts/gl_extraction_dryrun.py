"""Dry run of GL account extraction on a folder of invoice PDFs.

    uv run python scripts/gl_extraction_dryrun.py <folder>
    uv run python scripts/gl_extraction_dryrun.py gl_tagged/20260818134851100

Runs the same OCR the pipeline uses (api.services.ocr_service.extract_document)
on every PDF in the given folder, then writes a text report with each
invoice's line-item descriptions and their extracted GL accounts, plus any
gl_mappings[] entries (fees/freight/discounts with GL codes).

Output is written to <folder>/gl_extraction_results.txt and also printed
to the console. Nothing is sent to Tekion. Read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from api.services.ocr_helpers import get_dealership_name, get_raw_line_items, get_vin
from api.services.ocr_service import extract_document


def _gl_mappings(ocr: dict) -> list[dict]:
    """Pull gl_mappings[] from the OCR result (fees/freight/discounts with GLs)."""
    raw = ocr.get("gl_mappings") or []
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "gl_account": row.get("gl_account") or "",
                "amount": row.get("amount") or "",
                "mapped_description": row.get("mapped_description") or "",
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="GL extraction dry run on a folder of invoices")
    ap.add_argument("folder", help="Path to a folder containing invoice PDFs/images")
    ap.add_argument(
        "--output",
        help="Path to write the text report (default: <folder>/gl_extraction_results.txt)",
    )
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a folder: {folder}")
        return 1

    docs = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    )
    if not docs:
        print(f"No PDFs/images found in {folder}")
        return 1

    out_path = Path(args.output) if args.output else folder / "gl_extraction_results.txt"

    lines: list[str] = []
    lines.append(f"GL Extraction Dry Run")
    lines.append(f"Folder: {folder}")
    lines.append(f"Invoices: {len(docs)}")
    lines.append("=" * 70)

    for i, doc_path in enumerate(docs, 1):
        lines.append("")
        lines.append(f"[{i}/{len(docs)}] {doc_path.name}")
        lines.append("-" * 70)

        try:
            ocr = extract_document(str(doc_path))
        except Exception as exc:
            lines.append(f"  OCR FAILED: {exc}")
            continue

        lines.append(f"  document_type : {ocr.get('document_type')!r}")
        lines.append(f"  vendor        : {(ocr.get('vendor') or {}).get('name', '(none)')}")
        lines.append(f"  dealership    : {get_dealership_name(ocr) or '(none)'}")
        lines.append(f"  VIN           : {get_vin(ocr) or '(none)'}")
        lines.append(f"  invoice_no    : {ocr.get('invoice_number') or '(none)'}")
        lines.append(f"  needs_review  : {(ocr.get('_validation') or {}).get('needs_review', False)}")

        # Line items with GL accounts
        raw_items = get_raw_line_items(ocr)
        lines.append("")
        lines.append(f"  Line items ({len(raw_items)}):")
        if not raw_items:
            lines.append("    (none)")
        for j, item in enumerate(raw_items):
            desc = item.get("description") or "(no description)"
            gl = item.get("glAccount") or "(no GL)"
            qty = item.get("qty") or ""
            total = item.get("totalPrice") or ""
            lines.append(f"    {j}. GL={gl!r:<12} desc={desc!r}  qty={qty}  total={total}")

        # gl_mappings (fees/freight/discounts with GL codes)
        mappings = _gl_mappings(ocr)
        lines.append("")
        lines.append(f"  GL mappings ({len(mappings)}):")
        if not mappings:
            lines.append("    (none)")
        for m in mappings:
            gl = m["gl_account"] or "(no GL)"
            amt = m["amount"] or "(no amount)"
            desc = m["mapped_description"] or "(no description)"
            lines.append(f"    GL={gl!r:<12} amount={amt!r:<10} desc={desc!r}")

    report = "\n".join(lines) + "\n"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
