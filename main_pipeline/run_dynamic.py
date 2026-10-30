#!/usr/bin/env python3
"""CLI runner for the dynamic-schema two-stage pipeline (pipeline.py).

Usage:
    python run_dynamic.py                          # process all PDFs/images in this folder
    python run_dynamic.py --all                    # same
    python run_dynamic.py "<file.pdf>"             # one file
    python run_dynamic.py a.pdf b.png              # several files
    python run_dynamic.py --out results            # write JSON to results/<name>.json
"""
import argparse
import json
import sys
from pathlib import Path

from pipeline import BASE_DIR, IMAGE_EXTS, get_client, extract_path, load_pages


def discover_files() -> list[Path]:
    exts = {".pdf"} | IMAGE_EXTS
    return sorted(p for p in BASE_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in exts)


def display(path: Path, doc: dict) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{path.name}\n{bar}")
    print(f"document_type : {doc.get('document_type', '?')}")

    # Print all top-level keys (dynamic — we don't know the shape)
    for k, v in doc.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict):
            print(f"\n{k}:")
            for kk, vv in v.items():
                print(f"    {kk}: {vv}")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"\n{k} ({len(v)} rows):")
            if len(v) <= 5:
                for row in v:
                    print(f"    {row}")
            else:
                for row in v[:3]:
                    print(f"    {row}")
                print(f"    ... ({len(v) - 3} more)")
        elif isinstance(v, list):
            print(f"{k}: {v}")
        else:
            print(f"{k}: {v}")

    val = doc.get("_validation") or {}
    flag = "⚠ NEEDS REVIEW" if doc.get("_needs_review") else "✓ checks passed" if val.get("checks") else "– no checks"
    print(f"\nVALIDATION: {flag}   checks={val.get('checks', {})}")
    if val.get("notes"):
        print(f"            {val['notes']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dynamic-schema two-stage extraction (pipeline.py)")
    ap.add_argument("files", nargs="*", help="files to process (default: all PDFs/images here)")
    ap.add_argument("--all", action="store_true", help="process every PDF/image in this folder")
    ap.add_argument("--out", default=None, metavar="DIR", help="write full JSON per file to DIR/<name>.json")
    args = ap.parse_args()

    if args.all or not args.files:
        paths = discover_files()
    else:
        paths = [Path(f) for f in args.files]

    paths = [p for p in paths if p.exists()]
    if not paths:
        print("no files to process", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to Vertex AI…", file=sys.stderr)
    client = get_client()

    summary = []
    for path in paths:
        print(f"\n→ extracting {path.name} …", file=sys.stderr)
        try:
            doc = extract_path(path, client)
        except Exception as e:
            print(f"  ✗ FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            summary.append((path.name, "ERROR"))
            continue
        display(path, doc)
        if out_dir:
            dest = out_dir / (path.stem + ".json")
            dest.write_text(json.dumps(doc, indent=2, default=str))
            print(f"  → wrote {dest}", file=sys.stderr)
        summary.append((path.name, "review" if doc.get("_needs_review") else "ok"))

    print("\n" + "=" * 78, file=sys.stderr)
    print("SUMMARY:", file=sys.stderr)
    for name, status in summary:
        print(f"  [{status:>6}] {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
