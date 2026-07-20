#!/usr/bin/env python3
"""
Build the dots.ocr invoice extraction + quality report.

The invoice PDFs have no ground truth, so this summarises what dots.ocr
extracted and flags quality signals derived PURELY from dots.ocr's own layout
output (no other model):
  * page count
  * layout category breakdown (Text/Title/Table/...)
  * number of Table blocks (dots.ocr emits tables as HTML)
  * total/amount-like lines captured (keyword grep over extracted text)
  * a sample of the extracted text/markdown

Outputs:
  <out>/text/<name>.md         concatenated reading-order markdown (all pages)
  <out>/INVOICE_REPORT.md      per-invoice summary + quality notes
"""
import json
import re
import argparse
from collections import Counter
from pathlib import Path

MONEY_RE = re.compile(r"(total|subtotal|amount|balance|due|tax|invoice\s*(no|#|total)|\$|\bUSD\b)", re.I)


def page_jsons(d: Path, name: str):
    """Return layout-JSON paths for a doc, page-ordered (handles single-image and multi-page PDF)."""
    single = d / f"{name}.json"
    if single.exists():
        return [single]
    pages = sorted(d.glob(f"{name}_page_*.json"),
                   key=lambda p: int(re.search(r"_page_(\d+)", p.name).group(1)))
    return pages


def page_md(d: Path, name: str, page_jsons_list):
    mds = []
    for jp in page_jsons_list:
        mp = jp.with_suffix(".md")
        if mp.exists():
            mds.append(mp.read_text(encoding="utf-8"))
    return mds


def load_cells(jp: Path):
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, list) else None


def analyse(jsons):
    cats = Counter()
    n_tables = 0
    money_lines = []
    n_cells = 0
    parse_fail = 0
    for jp in jsons:
        cells = load_cells(jp)
        if cells is None:
            parse_fail += 1
            continue
        for c in cells:
            if not isinstance(c, dict):
                continue
            n_cells += 1
            cat = c.get("category", "?")
            cats[cat] += 1
            if cat == "Table":
                n_tables += 1
            text = str(c.get("text", ""))
            for line in re.split(r"<br\s*/?>|\n", text):
                line = re.sub(r"<[^>]+>", " ", line)
                line = re.sub(r"\s+", " ", line).strip()
                if line and MONEY_RE.search(line):
                    money_lines.append(line)
    return {
        "n_cells": n_cells, "cats": cats, "n_tables": n_tables,
        "money_lines": money_lines, "parse_fail": parse_fail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/home/ubuntu/ocr_testing/results/dots_ocr/invoices/raw")
    ap.add_argument("--out", default="/home/ubuntu/ocr_testing/results/dots_ocr/invoices")
    args = ap.parse_args()

    raw = Path(args.raw)
    out = Path(args.out)
    text_dir = out / "text"
    text_dir.mkdir(parents=True, exist_ok=True)

    subdirs = sorted([d for d in raw.iterdir() if d.is_dir()]) if raw.exists() else []
    if not subdirs:
        print(f"No dots.ocr output under {raw}")
        return

    md = []
    md.append("# Invoice OCR Extraction Report — dots.ocr\n")
    md.append("**Date:** 2026-06-11  ")
    md.append("**Pipeline:** dots.ocr (1.7B VLM) served via vLLM on a Tesla T4, `prompt_layout_all_en`  ")
    md.append(f"**Documents:** {len(subdirs)} invoice PDFs (no ground truth — qualitative report)\n")
    md.append("## Method\n")
    md.append(
        "dots.ocr parses each page into layout cells (bbox + category + text), with "
        "tables emitted as HTML. There is no ground truth for these invoices, so this "
        "report summarises **what was extracted** and surfaces quality signals derived "
        "only from dots.ocr's own output: page count, category mix, number of detected "
        "tables, and total/amount-like lines captured. Full reading-order markdown per "
        "invoice is in `text/<name>.md`; raw layout JSON + annotated page images are in "
        "`raw/<name>/`.\n")

    summary_rows = []
    for d in subdirs:
        name = d.name
        jsons = page_jsons(d, name)
        mds = page_md(d, name, jsons)
        full_md = ("\n\n".join(mds)).strip()
        (text_dir / f"{name}.md").write_text(full_md or "(no text extracted)", encoding="utf-8")

        a = analyse(jsons)
        n_pages = len(jsons)
        money = a["money_lines"]
        summary_rows.append((name, n_pages, a["n_tables"], a["n_cells"], len(money)))

        md.append(f"\n---\n\n## {name}\n")
        md.append(f"- **Pages:** {n_pages}")
        md.append(f"- **Layout cells:** {a['n_cells']}")
        md.append(f"- **Tables detected:** {a['n_tables']}")
        if a["parse_fail"]:
            md.append(f"- ⚠️ **Unparseable pages (model JSON fallback):** {a['parse_fail']}")
        cat_str = ", ".join(f"{k}: {v}" for k, v in a["cats"].most_common())
        md.append(f"- **Category breakdown:** {cat_str or '—'}")
        md.append(f"- **Total/amount-like lines captured:** {len(money)}")
        if money:
            md.append("\n  Sample money/total lines:")
            for line in money[:8]:
                md.append(f"  - `{line[:120]}`")
        sample = re.sub(r"\n{3,}", "\n\n", full_md)[:700]
        md.append("\n**Extracted text sample:**\n")
        md.append("```")
        md.append(sample if sample else "(no text extracted)")
        md.append("```")

    # ---- summary table at top-ish (append a quick-look table)
    table = ["\n---\n\n## Quick-look summary\n",
             "| Invoice | Pages | Tables | Cells | Money lines |",
             "|---|---|---|---|---|"]
    for name, p, t, c, m in summary_rows:
        table.append(f"| {name} | {p} | {t} | {c} | {m} |")
    # insert summary right after the Method section
    insert_at = next((i for i, l in enumerate(md) if l.startswith("\n---\n\n## ")), len(md))
    md = md[:insert_at] + table + md[insert_at:]

    (out / "INVOICE_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote report for {len(subdirs)} invoices -> {out/'INVOICE_REPORT.md'}")
    for name, p, t, c, m in summary_rows:
        print(f"  {name}: pages={p} tables={t} cells={c} money_lines={m}")


if __name__ == "__main__":
    main()
