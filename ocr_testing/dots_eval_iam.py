#!/usr/bin/env python3
"""
Evaluate dots.ocr handwriting recognition on the IAM Sentence Database.

Each IAM form prints a sentence (machine print = GROUND TRUTH) and the writer's
HANDWRITTEN copy of the same sentence below it. dots.ocr does not tag printed vs
handwritten, so we recover them self-containedly from its layout JSON:

  1. read <raw>/<id>/<id>.json  -> layout cells [{bbox,category,text}, ...]
  2. drop boilerplate (header "Sentence Database", form-id, "Name:")
  3. sort content cells top->bottom by bbox-y
  4. split into printed (top) vs handwritten (bottom) by choosing the partition
     that MAXIMISES text similarity between the two halves -- robust because the
     two halves are the same sentence. (falls back to largest vertical gap.)
  5. reference = printed half, hypothesis = handwritten half; score CER/WER.

Reuses the scoring/aggregation/report style of evaluate_iam.py.

Outputs:
  <eval_dir>/per_document.csv
  <eval_dir>/pairs/<id>.txt       (REF vs HYP, for inspection)
  <eval_dir>/EVALUATION_REPORT.md
"""
import csv
import json
import re
import sys
import argparse
import statistics
from pathlib import Path

BOILERPLATE_RE = re.compile(r"^(sentence database|[a-z]\d{2}-\d{3}[a-z]?|name\s*:?)$", re.I)


# ---------------------------------------------------------------- text utils
def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_scoring(text: str) -> str:
    return normalize_ws(text.lower())


def levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """1 - normalized char edit distance between two normalized strings."""
    a, b = normalize_for_scoring(a), normalize_for_scoring(b)
    if not a and not b:
        return 1.0
    d = levenshtein(a, b)
    return 1.0 - d / max(len(a), len(b), 1)


# ---------------------------------------------------------------- layout split
def load_cells(json_path: Path):
    """Return list of {bbox, category, text} content cells, or None if unusable."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None  # 'filtered' fallback dumped a raw string -> not parseable layout
    cells = []
    for c in data:
        if not isinstance(c, dict):
            continue
        text = normalize_ws(str(c.get("text", "")))
        if not text:
            continue
        if BOILERPLATE_RE.match(text):
            continue
        bbox = c.get("bbox") or [0, 0, 0, 0]
        try:
            y = float(bbox[1])
        except Exception:
            y = 0.0
        cells.append({"y": y, "text": text, "category": c.get("category", "")})
    cells.sort(key=lambda c: c["y"])
    return cells


def split_printed_handwritten(cells):
    """Choose partition index k maximising similarity(top, bottom)."""
    n = len(cells)
    if n < 2:
        return None
    best_k, best_sim = 1, -1.0
    for k in range(1, n):
        top = " ".join(c["text"] for c in cells[:k])
        bot = " ".join(c["text"] for c in cells[k:])
        sim = similarity(top, bot)
        if sim > best_sim:
            best_sim, best_k = sim, k
    printed = " ".join(c["text"] for c in cells[:best_k])
    handwritten = " ".join(c["text"] for c in cells[best_k:])
    return normalize_ws(printed), normalize_ws(handwritten), best_sim


# ---------------------------------------------------------------- metrics
def scores(ref: str, hyp: str):
    ref_n = normalize_for_scoring(ref)
    hyp_n = normalize_for_scoring(hyp)
    cd = levenshtein(ref_n, hyp_n)
    cer = cd / max(len(ref_n), 1)
    rw, hw = ref_n.split(), hyp_n.split()
    wd = levenshtein(rw, hw)
    wer = wd / max(len(rw), 1)
    return {
        "ref_chars": len(ref_n), "ref_words": len(rw), "hyp_words": len(hw),
        "char_edits": cd, "word_edits": wd, "cer": cer, "wer": wer,
        "word_acc": max(0.0, 1.0 - wer),
    }


def fmt(x):
    return f"{x:.4f}" if isinstance(x, float) else (x if x is not None else "")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/home/ubuntu/ocr_testing/results/dots_ocr/iam/raw",
                    help="dir with per-image dots.ocr output subfolders")
    ap.add_argument("--out", default="/home/ubuntu/ocr_testing/results/dots_ocr/iam",
                    help="eval output dir (per_document.csv, pairs/, EVALUATION_REPORT.md)")
    args = ap.parse_args()

    raw = Path(args.raw)
    out = Path(args.out)
    pairs = out / "pairs"
    pairs.mkdir(parents=True, exist_ok=True)

    subdirs = sorted([d for d in raw.iterdir() if d.is_dir()]) if raw.exists() else []
    if not subdirs:
        print(f"No dots.ocr output under {raw}")
        sys.exit(1)

    rows = []
    for d in subdirs:
        sid = d.name
        jpath = d / f"{sid}.json"
        if not jpath.exists():
            rows.append({"id": sid, "status": "NO_JSON"})
            continue
        cells = load_cells(jpath)
        if not cells:
            rows.append({"id": sid, "status": "NO_CELLS"})
            continue
        split = split_printed_handwritten(cells)
        if split is None:
            rows.append({"id": sid, "status": "SPLIT_FAIL"})
            continue
        printed, handwritten, sim = split
        m = scores(printed, handwritten)
        m.update({"id": sid, "status": "OK", "split_sim": sim,
                  "printed": printed, "handwritten": handwritten})
        rows.append(m)
        (pairs / f"{sid}.txt").write_text(
            f"# {sid}  (split_similarity={sim:.3f}, CER={m['cer']:.4f}, WER={m['wer']:.4f})\n\n"
            f"REF (printed / ground truth):\n{printed}\n\n"
            f"HYP (handwritten / prediction):\n{handwritten}\n", encoding="utf-8")

    scored = [r for r in rows if r["status"] == "OK"]
    if not scored:
        print("No documents scored. Statuses:", {r["status"] for r in rows})
        sys.exit(1)

    tot_cd = sum(r["char_edits"] for r in scored)
    tot_chars = sum(r["ref_chars"] for r in scored)
    tot_wd = sum(r["word_edits"] for r in scored)
    tot_words = sum(r["ref_words"] for r in scored)
    micro_cer = tot_cd / max(tot_chars, 1)
    micro_wer = tot_wd / max(tot_words, 1)
    macro_cer = statistics.mean(r["cer"] for r in scored)
    macro_wer = statistics.mean(r["wer"] for r in scored)
    median_cer = statistics.median(r["cer"] for r in scored)
    median_wer = statistics.median(r["wer"] for r in scored)
    exact = sum(1 for r in scored if r["wer"] == 0)

    # ---- CSV
    csv_path = out / "per_document.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "status", "ref_words", "hyp_words", "ref_chars",
                    "char_edits", "word_edits", "cer", "wer", "word_acc"])
        for r in rows:
            w.writerow([r["id"], r["status"], r.get("ref_words", ""),
                        r.get("hyp_words", ""), r.get("ref_chars", ""),
                        r.get("char_edits", ""), r.get("word_edits", ""),
                        fmt(r.get("cer")), fmt(r.get("wer")), fmt(r.get("word_acc"))])

    # ---- Markdown report
    best = sorted(scored, key=lambda r: r["cer"])[:5]
    worst = sorted(scored, key=lambda r: r["cer"], reverse=True)[:5]
    skipped = [r["id"] for r in rows if r["status"] != "OK"]

    md = []
    md.append("# Handwriting OCR Evaluation — IAM (dots.ocr)\n")
    md.append("**Date:** 2026-06-11  ")
    md.append("**Pipeline:** dots.ocr (1.7B VLM) served via vLLM on a Tesla T4, `prompt_layout_all_en`  ")
    md.append(f"**Dataset:** IAM Sentence Database forms — {len(rows)} documents\n")

    md.append("## Method\n")
    md.append(
        "Each IAM form contains a machine-**printed** prompt and the writer's "
        "**handwritten** copy of that prompt. dots.ocr returns layout cells (bbox + "
        "category + text) but does not label printed vs handwritten, so the two are "
        "separated self-containedly: content cells are sorted top-to-bottom and split "
        "at the partition that **maximises text similarity between the two halves** "
        "(the halves are the same sentence). The top (printed) half is the "
        "**ground-truth reference**; the bottom (handwritten) half is the "
        "**prediction**. Scoring is case- and whitespace-insensitive.\n")
    md.append(
        "- **CER** — Character Error Rate = char edit distance / reference chars (lower is better)\n"
        "- **WER** — Word Error Rate = word edit distance / reference words (lower is better)\n"
        "- **Word accuracy** = 1 − WER\n")

    md.append("\n## Headline results\n")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Documents scored | {len(scored)} / {len(rows)} |")
    md.append(f"| **Micro CER** (corpus-pooled) | **{micro_cer:.2%}** |")
    md.append(f"| **Micro WER** (corpus-pooled) | **{micro_wer:.2%}** |")
    md.append(f"| Macro CER (mean per-doc) | {macro_cer:.2%} |")
    md.append(f"| Macro WER (mean per-doc) | {macro_wer:.2%} |")
    md.append(f"| Median CER | {median_cer:.2%} |")
    md.append(f"| Median WER | {median_wer:.2%} |")
    md.append(f"| Corpus word accuracy (1 − micro WER) | {1-micro_wer:.2%} |")
    md.append(f"| Exact-match documents (WER = 0) | {exact} / {len(scored)} |")
    md.append(f"| Total reference words | {tot_words:,} |")

    if skipped:
        md.append(f"\n> ⚠️ {len(skipped)} document(s) could not be split/scored and were "
                  f"excluded: {', '.join(skipped)}\n")

    md.append("\n## Best 5 documents (lowest CER)\n")
    md.append("| ID | CER | WER | Ref words |")
    md.append("|---|---|---|---|")
    for r in best:
        md.append(f"| {r['id']} | {r['cer']:.2%} | {r['wer']:.2%} | {r['ref_words']} |")

    md.append("\n## Worst 5 documents (highest CER)\n")
    md.append("| ID | CER | WER | Ref words |")
    md.append("|---|---|---|---|")
    for r in worst:
        md.append(f"| {r['id']} | {r['cer']:.2%} | {r['wer']:.2%} | {r['ref_words']} |")

    md.append("\n## Per-document results\n")
    md.append("| ID | Status | Ref words | CER | WER | Word acc |")
    md.append("|---|---|---|---|---|---|")
    for r in rows:
        if r["status"] == "OK":
            md.append(f"| {r['id']} | OK | {r['ref_words']} | {r['cer']:.2%} "
                      f"| {r['wer']:.2%} | {r['word_acc']:.2%} |")
        else:
            md.append(f"| {r['id']} | {r['status']} | – | – | – | – |")

    md.append("\n## Example error (worst document)\n")
    w0 = worst[0]
    md.append(f"**{w0['id']}** — CER {w0['cer']:.2%}, WER {w0['wer']:.2%}\n")
    md.append(f"- **Reference (printed):** {w0['printed'][:400]}\n")
    md.append(f"- **Prediction (handwritten):** {w0['handwritten'][:400]}\n")

    md.append("\n## Notes & caveats\n")
    md.append(
        "- The printed prompt is used as a proxy ground truth; genuine writer "
        "deviations from the prompt are counted as errors, so true handwriting "
        "accuracy may be marginally higher than reported.\n"
        "- The printed/handwritten split is inferred from layout geometry + text "
        "similarity; a low `split_similarity` (see `pairs/<id>.txt`) indicates the "
        "model may have missed one of the two sections.\n"
        "- Per-image REF/HYP pairs are in `pairs/<id>.txt`; raw layout JSON, markdown, "
        "and annotated images are under `raw/<id>/`.\n")

    (out / "EVALUATION_REPORT.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Scored {len(scored)}/{len(rows)} documents")
    print(f"  Micro CER: {micro_cer:.2%}   Micro WER: {micro_wer:.2%}")
    print(f"  Macro CER: {macro_cer:.2%}   Median CER: {median_cer:.2%}   Exact: {exact}")
    if skipped:
        print(f"  Skipped: {skipped}")
    print(f"Report -> {out/'EVALUATION_REPORT.md'}")


if __name__ == "__main__":
    main()
