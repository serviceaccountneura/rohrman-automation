"""Dry run of the sublet VIN -> RO -> job-matching flow.

    uv run python scripts/sublet_job_match_dryrun.py <pdf-or-image>
    uv run python scripts/sublet_job_match_dryrun.py <pdf> --vin 5J6RS4H46TL012280
    uv run python scripts/sublet_job_match_dryrun.py <pdf> --dealership "Honda of Tysons"

Runs the same OCR the pipeline uses (api.services.ocr_service.extract_document),
pulls only the line-item descriptions out of the result, then asks Tekion for
the most recent open RO on that VIN and every job on it. The LLM matcher
(api.services.job_matching.match_line_items_to_jobs) is then called with those
descriptions and jobs, and the script prints — for each invoice line — which
jobNumber the LLM picked, alongside that job's capture (customer concern) and
techStory (technician notes) so you can eyeball whether the match is right.

Nothing is written to Tekion. No PO, pre-invoice, or draft is created. This
is read-only and intended for validating the matcher on real invoices.

If OCR cannot read a VIN, pass --vin explicitly. If OCR cannot read a
dealership, pass --dealership explicitly (otherwise the first dealer the
client knows about is used, since dealer context is required for the VIN
search to return ROs from the right store).
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

from api.services.job_matching import match_line_items_to_jobs
from api.services.ocr_helpers import get_dealership_name, get_raw_line_items, get_vin
from api.services.ocr_service import extract_document
from api.services.tekion_client import TekionApiClient


def _rule(title: str) -> None:
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sublet VIN -> RO -> job-match dry run")
    ap.add_argument("document", help="Path to the invoice PDF/image to OCR")
    ap.add_argument("--vin", help="Override the VIN (skips OCR VIN extraction)")
    ap.add_argument(
        "--dealership",
        help="Override the dealership name (required for Tekion dealer context)",
    )
    ap.add_argument(
        "--any-status",
        action="store_true",
        help="Pick the most recent RO regardless of status (testing only — "
             "production flow always filters to open ROs)",
    )
    args = ap.parse_args()

    doc_path = Path(args.document)
    if not doc_path.exists():
        print(f"File not found: {doc_path}")
        return 1

    # ── 1. OCR (same call the pipeline makes) ────────────────────────────────
    _rule(f"1. OCR — {doc_path.name}")
    print(f"  running vision_extract on {doc_path} ...")
    ocr = extract_document(str(doc_path))
    print(f"  document_type : {ocr.get('document_type')!r}")
    print(f"  pages         : {ocr.get('_pages')}")
    print(f"  needs_review  : {(ocr.get('_validation') or {}).get('needs_review', False)}")

    line_items = get_raw_line_items(ocr)
    descriptions = [item["description"] or "(no description)" for item in line_items]
    print(f"  line_items    : {len(descriptions)}")
    if not descriptions:
        print("  No line items found by OCR. Nothing to match.")
        return 0

    vin = (args.vin or get_vin(ocr) or "").strip().upper()
    dealership = (args.dealership or get_dealership_name(ocr) or "").strip()

    _rule("2. OCR-extracted fields")
    print(f"  VIN         : {vin or '(none)'}")
    print(f"  dealership  : {dealership or '(none)'}")
    print("  line descriptions:")
    for i, d in enumerate(descriptions):
        print(f"    {i}. {d}")

    if not vin:
        print("\n  No VIN — pass --vin to supply one. Cannot search Tekion without it.")
        return 1

    # ── 3. Login + dealer context ────────────────────────────────────────────
    _rule("3. Tekion login")
    client = TekionApiClient()  # no DB session — isolated, session not persisted
    client.login()
    print("  logged in")

    if dealership:
        dealer_id = client.find_dealer_by_name(dealership)
        if not dealer_id:
            print(f"  Could not match dealership {dealership!r} to a Tekion dealer.")
            return 1
        client.switch_dealer(dealer_id)
        print(f"  switched to dealer {dealer_id} ({dealership})")
    else:
        print("  No dealership resolved from OCR or args. VIN search may return no ROs.")
        dealer_id = ""

    # ── 4. Find the latest open RO for the VIN ───────────────────────────────
    _rule(f"4. Search ROs by VIN {vin}")
    hits = client.search_ro_by_vin(vin)
    print(f"  {len(hits)} RO(s) returned (newest first):")
    for h in hits:
        print(f"    roNo={h.get('roNo')!r}  status={h.get('status')!r}  "
              f"createdTime={h.get('createdTime')}  id={h.get('id')}")

    if args.any_status:
        ro = hits[0] if hits else None
        if ro:
            print("\n  --any-status: picking most recent RO regardless of status")
    else:
        ro = client.find_latest_open_ro_by_vin(vin)
    if not ro:
        print("\n  No open RO found for that VIN (all hits are INVOICED/CLOSED/VOIDED).")
        print("  Re-run with --any-status to test matching against closed ROs.")
        return 0
    print(f"\n  selected RO: roNo={ro.get('roNo')!r}  id={ro.get('id')}  "
          f"status={ro.get('status')!r}")

    # ── 5. Pull every job on that RO ─────────────────────────────────────────
    _rule(f"5. Jobs on RO {ro.get('roNo')}")
    jobs = client.get_ro_job_details(ro["id"])
    if not jobs:
        print("  No jobs returned for that RO.")
        return 0
    print(f"  {len(jobs)} job(s):")
    for j in jobs:
        print(f"    jobNumber={j['jobNumber']!r}")
        print(f"      capture  : {j.get('capture') or '(none)'}")
        print(f"      techStory: {j.get('techStory') or '(none)'}")

    # ── 6. LLM matching ──────────────────────────────────────────────────────
    _rule("6. LLM line-item -> job matching")
    print(f"  calling match_line_items_to_jobs with {len(descriptions)} line(s) "
          f"and {len(jobs)} job(s) ...")
    job_numbers = match_line_items_to_jobs(descriptions, jobs)

    # ── 7. Verdict ───────────────────────────────────────────────────────────
    _rule("7. Match results")
    by_number = {j["jobNumber"]: j for j in jobs}
    for i, desc in enumerate(descriptions):
        picked = job_numbers[i] if i < len(job_numbers) else ""
        job = by_number.get(picked, {})
        print(f"\n  line {i}: {desc!r}")
        print(f"    -> jobNumber {picked!r}")
        print(f"       capture  : {job.get('capture') or '(none)'}")
        print(f"       techStory: {job.get('techStory') or '(none)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
