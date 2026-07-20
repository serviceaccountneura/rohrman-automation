#!/usr/bin/env python3
"""
Evaluate the handwriting-OCR pipeline on the IAM 'iam_new' results.

Each results/iam_new/<id>.txt holds, for one IAM form:
  * the PRINTED prompt text  -> used as ground-truth reference
  * the HANDWRITTEN transcription the model produced (each word tagged
    "(handwritten)")            -> the prediction under test

The writer copied the printed prompt by hand, so the printed text is the
reference against which we score the model's handwriting transcription.

Outputs:
  * results/iam_new_eval/per_document.csv
  * results/iam_new_eval/EVALUATION_REPORT.md
"""

import csv
import re
import statistics
from pathlib import Path

RESULTS_DIR = Path("/home/ubuntu/ocr_testing/results/iam_new")
OUT_DIR = Path("/home/ubuntu/ocr_testing/results/iam_new_eval")

HW_TAG = "(handwritten)"


# ---------------------------------------------------------------- parsing
def parse_txt(path: Path):
    """Return (printed_reference, handwritten_prediction) strings."""
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in raw_lines if ln.strip() != ""]

    # Drop the "Sentence Database" header and the form-id line (e.g. A01-000).
    body = []
    for ln in lines:
        s = ln.strip()
        if s == "Sentence Database":
            continue
        if re.fullmatch(r"[A-Z]\d{2}-\d{3}[a-z]?", s):
            continue
        body.append(ln)

    # Split into printed (before first tagged line) and handwritten (rest).
    first_hw = next((i for i, ln in enumerate(body) if HW_TAG in ln), None)
    if first_hw is None:
        printed_lines, hw_lines = body, []
    else:
        printed_lines, hw_lines = body[:first_hw], body[first_hw:]

    printed = " ".join(printed_lines)

    hw = " ".join(hw_lines).replace(HW_TAG, " ")
    # Strip a trailing "Name:" form-field label that follows the handwriting.
    hw = re.sub(r"\bName\s*:\s*$", "", hw)
    printed = re.sub(r"\bName\s*:\s*$", "", printed)

    return normalize_ws(printed), normalize_ws(hw)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_scoring(text: str) -> str:
    """Lowercase + collapse whitespace for case/spacing-insensitive scoring."""
    return normalize_ws(text.lower())


# ---------------------------------------------------------------- metrics
def levenshtein(a, b):
    """Edit distance over sequences a and b (chars or word-tokens)."""
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


def scores(ref: str, hyp: str):
    ref_n = normalize_for_scoring(ref)
    hyp_n = normalize_for_scoring(hyp)

    # Character Error Rate
    cd = levenshtein(ref_n, hyp_n)
    cer = cd / max(len(ref_n), 1)

    # Word Error Rate
    rw, hw = ref_n.split(), hyp_n.split()
    wd = levenshtein(rw, hw)
    wer = wd / max(len(rw), 1)

    # Simple word-level accuracy / similarity
    word_acc = max(0.0, 1.0 - wer)
    return {
        "ref_chars": len(ref_n),
        "ref_words": len(rw),
        "hyp_words": len(hw),
        "char_edits": cd,
        "word_edits": wd,
        "cer": cer,
        "wer": wer,
        "word_acc": word_acc,
    }


# ---------------------------------------------------------------- main
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(RESULTS_DIR.glob("*.txt")):
        printed, hw = parse_txt(path)
        if not printed or not hw:
            rows.append({
                "id": path.stem, "status": "MISSING_SECTION",
                "ref_words": len((printed or "").split()),
                "hyp_words": len((hw or "").split()),
                "cer": "", "wer": "", "word_acc": "",
                "char_edits": "", "word_edits": "", "ref_chars": len(printed or ""),
                "printed": printed, "handwritten": hw,
            })
            continue
        m = scores(printed, hw)
        m.update({"id": path.stem, "status": "OK",
                  "printed": printed, "handwritten": hw})
        rows.append(m)

    scored = [r for r in rows if r["status"] == "OK"]

    # ---- aggregate (micro = pooled edits/length; macro = mean of per-doc)
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
    csv_path = OUT_DIR / "per_document.csv"
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

    md = []
    md.append("# Handwriting OCR Evaluation — IAM (`iam_new`)\n")
    md.append(f"**Date:** 2026-06-08  ")
    md.append(f"**Pipeline:** Gemini 2.5 Flash on Vertex AI (`main.py`)  ")
    md.append(f"**Dataset:** IAM Sentence Database forms — {len(rows)} documents\n")

    md.append("## Method\n")
    md.append(
        "Each IAM form contains a machine-**printed** prompt and the writer's "
        "**handwritten** copy of that prompt. The pipeline transcribes both. "
        "The printed transcription is used as the **ground-truth reference**, "
        "and the model's **handwritten** transcription is the prediction under "
        "test. Scoring is case- and whitespace-insensitive.\n")
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

    if len(rows) - len(scored):
        skipped = [r['id'] for r in rows if r['status'] != 'OK']
        md.append(f"\n> ⚠️ {len(skipped)} document(s) had no usable handwritten "
                  f"section and were excluded: {', '.join(skipped)}\n")

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
            md.append(f"| {r['id']} | {r['status']} | {r.get('ref_words','')} | – | – | – |")

    md.append("\n## Example error (worst document)\n")
    w0 = worst[0]
    md.append(f"**{w0['id']}** — CER {w0['cer']:.2%}, WER {w0['wer']:.2%}\n")
    md.append(f"- **Reference (printed):** {w0['printed'][:400]}\n")
    md.append(f"- **Prediction (handwritten):** {w0['handwritten'][:400]}\n")

    md.append("\n## Notes & caveats\n")
    md.append(
        "- The printed prompt is used as a proxy ground truth. Genuine writer "
        "deviations (a writer who wrote different words than the prompt) are "
        "counted as errors, so true handwriting-recognition accuracy may be "
        "marginally higher than reported.\n"
        "- Spacing around punctuation (e.g. `Foot-Griffiths` vs `Foot - Griffiths`) "
        "and truncated final lines are counted as errors.\n"
        "- The companion `.json` files are inconsistent (several are empty or "
        "`UNRECOGNIZED`) and were not used for scoring; the `.txt` outputs are "
        "the reliable source.\n")

    (OUT_DIR / "EVALUATION_REPORT.md").write_text("\n".join(md), encoding="utf-8")

    # ---- console summary
    print(f"Scored {len(scored)}/{len(rows)} documents")
    print(f"  Micro CER: {micro_cer:.2%}   Micro WER: {micro_wer:.2%}")
    print(f"  Macro CER: {macro_cer:.2%}   Macro WER: {macro_wer:.2%}")
    print(f"  Median CER: {median_cer:.2%}  Exact-match docs: {exact}")
    print(f"Report -> {OUT_DIR/'EVALUATION_REPORT.md'}")
    print(f"CSV    -> {csv_path}")


def fmt(x):
    return f"{x:.4f}" if isinstance(x, float) else (x if x is not None else "")


if __name__ == "__main__":
    main()
