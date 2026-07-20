#!/usr/bin/env python3
"""
dots.ocr inference runner (vLLM backend).

Talks to a vLLM server serving the dots.ocr model (default localhost:8000,
served-model-name "model") via the repo's DotsOCRParser. For each input image
or PDF it writes, under <out>/<name>/:
    <name>.json        layout cells [{bbox,category,text}, ...]  (per page for PDFs: <name>_page_N.json)
    <name>.md          reading-order markdown (tables as HTML)
    <name>_nohf.md     markdown without page header/footer
    <name>.jpg         annotated layout image
and an <out>/<name>.jsonl manifest.

Usage:
    venvs/dots/bin/python dots_run.py <dir_or_files...> --out <outdir> [--max-pixels N] [--dpi N] [--threads N]
"""
import sys
import time
import argparse
from pathlib import Path

from dots_ocr import DotsOCRParser

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
VALID = IMAGE_EXTS | {".pdf"}


def collect_files(args):
    files = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files += [f for f in sorted(p.iterdir()) if f.suffix.lower() in VALID]
        elif p.is_file() and p.suffix.lower() in VALID:
            files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="image/PDF files or directories")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--ip", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model-name", default="model")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--max-pixels", type=int, default=None,
                    help="cap input pixels (e.g. 4000000 for big IAM scans -> faster)")
    ap.add_argument("--max-tokens", type=int, default=12000,
                    help="max completion tokens; must be < server --max-model-len minus input tokens")
    ap.add_argument("--prompt", default="prompt_layout_all_en")
    ap.add_argument("--limit", type=int, default=None, help="process only first N files (smoke test)")
    args = ap.parse_args()

    files = collect_files(args.inputs)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("No valid input files.")
        sys.exit(1)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    parser = DotsOCRParser(
        ip=args.ip,
        port=args.port,
        model_name=args.model_name,
        dpi=args.dpi,
        num_thread=args.threads,
        max_pixels=args.max_pixels,
        max_completion_tokens=args.max_tokens,
        output_dir=str(out),
        use_hf=False,
    )

    print(f"Processing {len(files)} file(s) -> {out}")
    ok, fail = 0, 0
    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {path.name}", flush=True)
        t0 = time.time()
        try:
            results = parser.parse_file(str(path), output_dir=str(out), prompt_mode=args.prompt)
            dt = time.time() - t0
            print(f"  ok  {dt:.1f}s  ({len(results)} page(s))", flush=True)
            ok += 1
        except Exception as e:
            dt = time.time() - t0
            print(f"  FAIL  {dt:.1f}s  {type(e).__name__}: {e}", flush=True)
            fail += 1
    print(f"\nDone. ok={ok} fail={fail} -> {out}")


if __name__ == "__main__":
    main()
