"""A second read of the fields that decide where money goes, with confidence.

TESTING BRANCH (staging-test-gemini-2.5pro-confidence-score).

Gemini 3.x does not report how sure it is of what it read (Vertex refuses
`response_logprobs` for it). Gemini 2.5 Pro does: for every token it returns
the probability of the token it chose and of the runners-up. So while 3.6
Flash reads the whole invoice, 2.5 Pro reads just the invoice number and the
GL lines a clerk wrote on it, and the probabilities of the characters inside
each value become that value's confidence.

A Honda invoice with "Tires #2430" on it was once read as #2410 and posted to
the wrong account. Here the "3" would come back with its probability -- and
"1", if the model weighed it, as an alternative.

`read()` never raises: a failure comes back as {"error": ...} and the main
read carries on alone.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from google.genai import types

from api.config import settings

PROMPT = """You are reading a scanned vendor invoice that a dealership clerk has annotated by hand.
Return only two things.

1. `invoice_number`: everything printed in the invoice-number field, as one value: the number,
   plus any short letter code printed with it in that field -- beside it on the same line
   ("6101029 RI") or directly beneath it ("331937" with "HOM" under it is "331937 HOM").
   Number first, parts joined by one space. "" when there is none.

2. `gl_lines`: each General Ledger account the clerk wrote or stamped on the invoice with an
   amount beside it, one entry per written line, in the order written:
   * `account`: the account number exactly as written, digits (and a trailing letter) only.
   * `amount`: the amount as written, as a plain number: no "$", no commas, a decimal point
     before the cents (raised or underlined cents become decimals), and a leading "-" only when
     a minus is written in front of the amount.
   * `label`: any word written beside the account ("Tires", "Freight"), or "".
   Printed GL codes in a line-item table are not clerk annotations: leave them out.
   [] when nothing is written.

Read every digit from the page itself. Do not guess or complete a number from what is usual."""


def _schema() -> types.Schema:
    s = types.Schema
    t = types.Type
    return s(
        type=t.OBJECT,
        properties={
            "invoice_number": s(type=t.STRING),
            "gl_lines": s(
                type=t.ARRAY,
                items=s(
                    type=t.OBJECT,
                    properties={
                        "account": s(type=t.STRING),
                        "amount": s(type=t.STRING),
                        "label": s(type=t.STRING),
                    },
                    required=["account", "amount"],
                ),
            ),
        },
        required=["invoice_number", "gl_lines"],
    )


def _pct(logprob: float | None) -> float:
    return round(math.exp(logprob) * 100, 2) if logprob is not None else 0.0


def _value_spans(text: str) -> dict[str, Any]:
    """Character spans of each value in the model's JSON text, in order.

    The JSON is parsed for the values; this finds where each one sits in the
    raw text so the tokens covering it can be picked out.
    """
    def spans(key: str) -> list[tuple[int, int]]:
        return [m.span(1) for m in re.finditer(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)]

    inv = spans("invoice_number")
    return {
        "invoice_number": inv[0] if inv else None,
        "account": spans("account"),
        "amount": spans("amount"),
        "label": spans("label"),
    }


def _field(value: str, span: tuple[int, int] | None, tokens: list[dict]) -> dict[str, Any]:
    """One value with the tokens that spell it and how sure the model was."""
    out: dict[str, Any] = {"value": value, "confidence": None, "lowest": None, "tokens": []}
    if span is None:
        return out
    start, end = span
    picked = []
    for tok in tokens:
        s, e = tok["start"], tok["end"]
        if e <= start or s >= end:
            continue
        part = tok["text"][max(start - s, 0): len(tok["text"]) - max(e - end, 0)]
        if not part:
            continue
        picked.append({"text": part, "confidence": tok["confidence"], "alternatives": tok["alternatives"]})
    out["tokens"] = picked
    if picked:
        low = min(picked, key=lambda p: p["confidence"])
        out["confidence"] = low["confidence"]
        out["lowest"] = low["text"]
    return out


def read(parts: list[types.Part], client: Any) -> dict[str, Any]:
    """Invoice number and handwritten GL lines from 2.5 Pro, with confidence per value.

    `parts` are the page images exactly as the main read gets them.
    """
    started = time.monotonic()
    model = settings.confidence_model
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[*parts, types.Part.from_text(text=PROMPT)],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_schema(),
                response_logprobs=True,
                logprobs=5,
                max_output_tokens=8192,
            ),
        )
        parsed = json.loads(resp.text or "{}")
        lp = resp.candidates[0].logprobs_result
    except Exception as e:  # noqa: BLE001 -- the main read must not fail because of this one
        return {"model": model, "error": str(e)[:300], "seconds": round(time.monotonic() - started, 1)}

    # Rebuild the output from its tokens, keeping where each one sits.
    tokens: list[dict] = []
    text = ""
    tops = (lp.top_candidates if lp else None) or []
    for i, chosen in enumerate((lp.chosen_candidates if lp else None) or []):
        tok = chosen.token or ""
        alts = []
        if i < len(tops):
            alts = [
                {"text": a.token, "confidence": _pct(a.log_probability)}
                for a in (tops[i].candidates or [])
                # Runners-up under 0.1% are noise ("_", " fifty"), not doubt.
                if a.token != tok and _pct(a.log_probability) >= 0.1
            ][:4]
        tokens.append({
            "text": tok,
            "start": len(text),
            "end": len(text) + len(tok),
            "confidence": _pct(chosen.log_probability),
            "alternatives": alts,
        })
        text += tok

    spans = _value_spans(text)
    lines = []
    for i, line in enumerate(parsed.get("gl_lines") or []):
        def at(key: str) -> tuple[int, int] | None:
            found = spans[key]
            return found[i] if i < len(found) else None

        lines.append({
            "account": _field(str(line.get("account") or ""), at("account"), tokens),
            "amount": _field(str(line.get("amount") or ""), at("amount"), tokens),
            "label": str(line.get("label") or ""),
        })

    return {
        "model": model,
        "seconds": round(time.monotonic() - started, 1),
        "invoice_number": _field(str(parsed.get("invoice_number") or ""), spans["invoice_number"], tokens),
        "gl_lines": lines,
        "tokens_matched": text == (resp.text or ""),
    }


def apply(doc: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Use 2.5 Pro's invoice number and GL lines in the main read.

    What 3.6 Flash read is kept under `flash` for comparison, and `used` says
    which model each field came from. A field 2.5 Pro returned empty keeps the
    Flash value: an empty answer is more likely a miss than a correction.
    """
    from api.services import ocr_helpers

    flash = {
        "invoice_number": ocr_helpers.get_invoice_number(doc),
        "gl_lines": [
            {"account": str(g.get("gl_account") or ""), "amount": str(g.get("amount") or "")}
            for g in doc.get("gl_mappings") or []
        ],
    }
    second["flash"] = flash
    used = {"invoice_number": "flash", "gl_lines": "flash"}
    if second.get("error"):
        second["used"] = used
        return doc

    inv = (second.get("invoice_number") or {}).get("value") or ""
    if inv:
        idents = [
            i for i in (doc.get("identifiers") or [])
            if "invoice" not in str(i.get("label") or "").lower()
            or "date" in str(i.get("label") or "").lower()
        ]
        doc["identifiers"] = [{"label": "Invoice Number", "value": inv}, *idents]
        used["invoice_number"] = settings.confidence_model

    lines = [
        ln for ln in second.get("gl_lines") or []
        if ln["account"]["value"] and ln["amount"]["value"]
    ]
    if lines:
        doc["gl_mappings"] = [
            {
                "gl_account": ln["account"]["value"],
                "amount": ln["amount"]["value"],
                "mapped_description": ln.get("label") or None,
            }
            for ln in lines
        ]
        # The sign is also read from the notes (ocr_helpers.gl_notes), so the
        # GL lines there are replaced too; every other note is kept.
        kept = [
            n for n in doc.get("handwritten_notes") or []
            if not any(ocr_helpers._NOTE_GL_LINE.match(x) for x in ocr_helpers.note_lines(n))
        ]
        doc["handwritten_notes"] = kept + [
            f"#{ln['account']['value']} {ln['amount']['value']}" for ln in lines
        ]
        used["gl_lines"] = settings.confidence_model
    second["used"] = used
    return doc
