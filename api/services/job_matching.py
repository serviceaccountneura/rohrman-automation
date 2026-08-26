"""Sublet line-item -> RO job matching.

Sublet invoices are matched to a repair order by VIN rather than by the RO
number printed on the invoice (see api/services/pipeline_service.py). Once
the RO is known, each job on it still needs to be identified so line items
post against the right job. The LLM reads each job's "Capture" text
(job.concern) and "Tech Story" text (job.operations[].storyLines[].text)
and decides which job each invoice line item belongs to.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

PROJECT_ROOT = MAIN_PIPELINE.parent
if not os.environ.get("VERTEX_CREDENTIALS"):
    creds = PROJECT_ROOT / "neura_vertex_ai.json"
    if creds.exists():
        os.environ["VERTEX_CREDENTIALS"] = str(creds)

from google.genai import types  # noqa: E402
from pipeline import get_client  # noqa: E402

JOB_MATCH_MODEL = "gemini-2.5-flash-lite"

_PROMPT = """You are matching a sublet vendor invoice's line items to the correct job on a repair order.

Each JOB below has:
- jobNumber: the job's identifier on the RO
- capture: the customer concern captured at write-up
- techStory: the technician's notes on what was done

JOBS:
{jobs}

INVOICE LINE ITEMS (0-indexed):
{items}

For each line item, pick the jobNumber whose capture/techStory best matches what the line item describes. If nothing matches well, use the jobNumber of job index 0.

Respond with ONLY a JSON array of jobNumber strings, one per line item, in order. Example: ["1", "1", "2"]"""


def match_line_items_to_jobs(
    line_descriptions: list[str],
    jobs: list[dict[str, Any]],
) -> list[str]:
    """Return the jobNumber to use for each line item, same order as input.

    Falls back to the first job's jobNumber for every item if the LLM call
    fails or the jobs list is empty/singular (nothing to disambiguate).
    """
    if not jobs:
        return ["" for _ in line_descriptions]
    default_job_number = jobs[0]["jobNumber"]
    if len(jobs) == 1 or not line_descriptions:
        return [default_job_number for _ in line_descriptions]

    jobs_text = "\n".join(
        f"- jobNumber={j['jobNumber']!r} capture={j.get('capture') or '(none)'!r} "
        f"techStory={j.get('techStory') or '(none)'!r}"
        for j in jobs
    )
    items_text = "\n".join(f"{i}. {d}" for i, d in enumerate(line_descriptions))
    prompt = _PROMPT.format(jobs=jobs_text, items=items_text)

    try:
        client = get_client()
        resp = client.models.generate_content(
            model=JOB_MATCH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                max_output_tokens=500,
            ),
        )
        answer = json.loads(resp.text or "[]")
        valid_job_numbers = {j["jobNumber"] for j in jobs}
        result = [
            str(a) if str(a) in valid_job_numbers else default_job_number
            for a in answer
        ]
        # Pad/truncate defensively in case the model returns the wrong length.
        if len(result) < len(line_descriptions):
            result += [default_job_number] * (len(line_descriptions) - len(result))
        return result[: len(line_descriptions)]
    except Exception as e:  # noqa: BLE001
        print(f"[JOB_MATCH] LLM matching failed ({e}), defaulting all items to job {default_job_number}")
        return [default_job_number for _ in line_descriptions]
