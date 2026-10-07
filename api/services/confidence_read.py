"""Three readers vote on the fields that decide where money goes.

TESTING BRANCH (staging-test-gemini-2.5pro-confidence-score).

The invoice number and every handwritten GL account and amount are read by
three independent readers:

    Gemini 3.6 Flash   the main read of the whole invoice
    Gemini 2.5 Pro     this module's read, with a confidence per character,
                       and a box around each value on the page
    PaddleOCR          a text-recognition model (not a language model) that
                       reads the crops inside 2.5 Pro's boxes -- see paddle_ocr/

A value two readers agree on is used. When no two agree, the document stops
with UNCLEAR_READING for a person to check, rather than posting a guess.

Why three: a single model can be completely sure of a misreading. On a Toyota
invoice 2.5 Pro read a handwritten 474.56 as 474.50 at 100% confidence; Flash
read 474.56. The per-character confidence did not catch it -- disagreement did.

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
read carries on alone, without a vote.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
import urllib.request
from decimal import Decimal, InvalidOperation
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

For every value also give where it is on the page, so another reader can check it:
   * `page`: the page number it is on, starting at 1.
   * `box_2d`: [ymin, xmin, ymax, xmax] normalised to 0-1000, drawn tightly around that value
     alone -- `invoice_number_box` around the invoice number, `account_box` around the
     handwritten account (with its # sign), `amount_box` around the handwritten amount
     (with its cents, raised or not).

Read every digit from the page itself. Do not guess or complete a number from what is usual."""


def _schema() -> types.Schema:
    s = types.Schema
    t = types.Type
    box = s(type=t.ARRAY, items=s(type=t.INTEGER))
    return s(
        type=t.OBJECT,
        properties={
            "invoice_number": s(type=t.STRING),
            "invoice_number_page": s(type=t.INTEGER),
            "invoice_number_box": box,
            "gl_lines": s(
                type=t.ARRAY,
                items=s(
                    type=t.OBJECT,
                    properties={
                        "account": s(type=t.STRING),
                        "amount": s(type=t.STRING),
                        "label": s(type=t.STRING),
                        "page": s(type=t.INTEGER),
                        "account_box": box,
                        "amount_box": box,
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


# ── PaddleOCR ────────────────────────────────────────────────────────────────

# The recognition model reads a line about 48 px high, so a crop from the
# enlarged, cleaned-up page is shrunk first: the same reading, a fraction of
# the bytes sent and the memory used.
_CROP_MAX_SIDE = 1000


def _crop(images: list, page: Any, box: Any):
    """The region 2.5 Pro boxed, with a margin so no stroke is cut off. None if unusable."""
    try:
        img = images[max(int(page or 1), 1) - 1]
        y0, x0, y1, x1 = [min(max(float(v), 0.0), 1000.0) / 1000 for v in box]
    except (IndexError, TypeError, ValueError):
        return None
    if y1 <= y0 or x1 <= x0:
        return None
    w, h = img.size
    ph, pw = (y1 - y0) * 0.2, (x1 - x0) * 0.08
    left, top = int(max(x0 - pw, 0) * w), int(max(y0 - ph, 0) * h)
    right, bottom = int(min(x1 + pw, 1) * w), int(min(y1 + ph, 1) * h)
    if right - left < 8 or bottom - top < 8:
        return None
    crop = img.crop((left, top, right, bottom)).convert("RGB")
    crop.thumbnail((_CROP_MAX_SIDE, _CROP_MAX_SIDE))
    return crop


def _paddle(crops: list) -> list[dict | None]:
    """PaddleOCR's reading of each crop, in order; None where it could not read."""
    url = os.environ.get("PADDLE_OCR_URL", "")
    usable = [c for c in crops if c is not None]
    if not url or not usable:
        return [None] * len(crops)
    payload = []
    for crop in usable:
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        payload.append(base64.b64encode(buf.getvalue()).decode())
    try:
        req = urllib.request.Request(
            f"{url}/read",
            data=json.dumps({"images": payload}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            results = json.loads(resp.read()).get("results") or []
    except Exception as e:  # noqa: BLE001 -- the vote goes ahead with two readers
        print(f"[OCR] PaddleOCR unavailable: {e}")
        return [None] * len(crops)
    it = iter(results)
    return [next(it, None) if c is not None else None for c in crops]


def read(images: list, parts: list[types.Part], client: Any) -> dict[str, Any]:
    """Invoice number and handwritten GL lines from 2.5 Pro and PaddleOCR.

    `images` and `parts` are the page images exactly as the main read gets
    them -- PIL images for cropping, and the same images as request parts.
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
    raw_lines = parsed.get("gl_lines") or []
    lines = []
    for i, line in enumerate(raw_lines):
        def at(key: str) -> tuple[int, int] | None:
            found = spans[key]
            return found[i] if i < len(found) else None

        lines.append({
            "account": _field(str(line.get("account") or ""), at("account"), tokens),
            "amount": _field(str(line.get("amount") or ""), at("amount"), tokens),
            "label": str(line.get("label") or ""),
        })

    # PaddleOCR reads what 2.5 Pro boxed: the invoice number, then each line's
    # account and amount.
    crops = [_crop(images, parsed.get("invoice_number_page"), parsed.get("invoice_number_box"))]
    for line in raw_lines:
        crops.append(_crop(images, line.get("page"), line.get("account_box")))
        crops.append(_crop(images, line.get("page"), line.get("amount_box")))
    paddle_started = time.monotonic()
    paddle = _paddle(crops)
    paddle_reads = {
        "invoice_number": paddle[0],
        "gl_lines": [
            {"account": paddle[1 + 2 * i], "amount": paddle[2 + 2 * i]} for i in range(len(raw_lines))
        ],
        "seconds": round(time.monotonic() - paddle_started, 1),
        "available": any(p is not None for p in paddle),
    }

    return {
        "model": model,
        "seconds": round(paddle_started - started, 1),
        "invoice_number": _field(str(parsed.get("invoice_number") or ""), spans["invoice_number"], tokens),
        "gl_lines": lines,
        "paddle": paddle_reads,
    }


# ── The vote ─────────────────────────────────────────────────────────────────

READERS = ("flash", "pro", "paddle")
READER_NAMES = {"flash": "Gemini 3.6 Flash", "pro": "Gemini 2.5 Pro", "paddle": "PaddleOCR"}


def _account_key(raw: str) -> str:
    """"#2430", "GL# 2430" and "2430" are the same account."""
    m = re.search(r"(\d{3,6}[A-Za-z]?)", str(raw or ""))
    return m.group(1).upper() if m else ""


def _amount_cents(raw: str) -> set[int]:
    """Every value in cents that a written amount can mean.

    A reading with a decimal point means one thing. Without one it may be
    whole dollars ("339") or have its cents raised with the point left out --
    PaddleOCR reads a handwritten $225^95 as "$22595" -- so both count.
    """
    text = str(raw or "").strip()
    negative = text.startswith("-") or text.startswith("(") or "CR" in text.upper()
    text = re.sub(r"[^\d.]", "", text)
    if not re.search(r"\d", text):
        return set()
    sign = -1 if negative else 1
    if "." in text:
        try:
            return {sign * int((Decimal(text.strip(".")) * 100).to_integral_value())}
        except InvalidOperation:
            return set()
    digits = text.lstrip("0") or "0"
    options = {sign * int(digits) * 100}
    if len(digits) >= 3:
        options.add(sign * int(digits))
    return options


def _invoice_key(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def _agree(kind: str, a: str, b: str) -> bool:
    if kind == "account":
        ka, kb = _account_key(a), _account_key(b)
        if not ka or not kb:
            return False
        # PaddleOCR reads a handwritten "#" as "1": "#2410" comes back "12410".
        return ka == kb or (len(ka) == len(kb) + 1 and ka == "1" + kb) or (
            len(kb) == len(ka) + 1 and kb == "1" + ka
        )
    if kind == "amount":
        return bool(_amount_cents(a) & _amount_cents(b))
    ka, kb = _invoice_key(a), _invoice_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # PaddleOCR's crop can take in part of the printed label ("er: 99100200901").
    longer, shorter = (ka, kb) if len(ka) > len(kb) else (kb, ka)
    return len(shorter) >= 4 and longer.endswith(shorter) and longer[: -len(shorter)].isalpha()


def _vote(kind: str, reads: dict[str, dict | None]) -> dict[str, Any]:
    """Which value two readers agree on, if any.

    `reads` maps each reader to {"value": ..., and its own score} or None
    when that reader had nothing. The winning value is reported in the form
    the Gemini readers write it ("225.95", not PaddleOCR's "$22595").
    """
    present = {r: v for r, v in reads.items() if v and str(v.get("value") or "").strip()}
    best: list[str] = []
    for reader, read in present.items():
        group = [o for o, other in present.items() if _agree(kind, read["value"], other["value"])]
        if len(group) > len(best):
            best = group
    agreed = len(best) >= 2
    final = ""
    if best:
        source = next((r for r in ("flash", "pro", "paddle") if r in best), best[0])
        final = str(present[source]["value"]).strip()
        if kind == "account":
            final = _account_key(final)
    return {
        "readers": reads,
        "final": final if agreed else "",
        "agreed_by": best if agreed else [],
        "agree": len(best),
        "of": len(present),
        "confident": agreed,
    }


def apply(doc: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Vote, and use what two readers agree on in the main read.

    Sets `second["votes"]`, and `second["unresolved"]` listing every value no
    two readers agreed on; the pipeline stops such a document (see
    pipeline_service, UNCLEAR_READING). If 2.5 Pro failed there is no vote
    and Flash's read stands on its own, as before this branch.
    """
    from api.services import ocr_helpers

    flash_lines = [
        {"account": str(g.get("gl_account") or ""), "amount": str(g.get("amount") or ""),
         "label": str(g.get("mapped_description") or "")}
        for g in doc.get("gl_mappings") or []
    ]
    flash_invoice = ocr_helpers.get_invoice_number(doc)
    second["flash"] = {"invoice_number": flash_invoice, "gl_lines": flash_lines}
    second["unresolved"] = []
    if second.get("error"):
        second["votes"] = None
        return doc

    paddle = second.get("paddle") or {}
    pro_lines = second.get("gl_lines") or []

    def pro_read(field: dict) -> dict | None:
        return {"value": field.get("value", ""), "confidence": field.get("confidence")} if field else None

    def paddle_read(p: dict | None) -> dict | None:
        return {"value": p.get("text", ""), "score": p.get("score")} if p else None

    invoice_vote = _vote("invoice", {
        "flash": {"value": flash_invoice} if flash_invoice else None,
        "pro": pro_read(second.get("invoice_number") or {}),
        "paddle": paddle_read(paddle.get("invoice_number")),
    })

    # Lines are matched by account where the readers agree on it, otherwise by
    # position; a line only one Gemini reader found still gets a slot.
    unmatched_flash = list(range(len(flash_lines)))
    line_votes = []
    for i, pro in enumerate(pro_lines):
        match = next((j for j in unmatched_flash
                      if _agree("account", flash_lines[j]["account"], pro["account"]["value"])), None)
        if match is None and i in unmatched_flash:
            match = i
        if match is not None:
            unmatched_flash.remove(match)
        flash = flash_lines[match] if match is not None else None
        paddle_lines = paddle.get("gl_lines") or []
        pad = paddle_lines[i] if i < len(paddle_lines) else {}
        line_votes.append({
            "label": pro.get("label") or (flash or {}).get("label", ""),
            "account": _vote("account", {
                "flash": {"value": flash["account"]} if flash else None,
                "pro": pro_read(pro["account"]),
                "paddle": paddle_read((pad or {}).get("account")),
            }),
            "amount": _vote("amount", {
                "flash": {"value": flash["amount"]} if flash else None,
                "pro": pro_read(pro["amount"]),
                "paddle": paddle_read((pad or {}).get("amount")),
            }),
        })
    for j in unmatched_flash:
        flash = flash_lines[j]
        line_votes.append({
            "label": flash["label"],
            "account": _vote("account", {"flash": {"value": flash["account"]}, "pro": None, "paddle": None}),
            "amount": _vote("amount", {"flash": {"value": flash["amount"]}, "pro": None, "paddle": None}),
        })

    second["votes"] = {"invoice_number": invoice_vote, "gl_lines": line_votes}

    def describe(vote: dict) -> str:
        said = [f"{READER_NAMES[r]} {vote['readers'][r]['value']!r}"
                for r in READERS if vote["readers"].get(r) and vote["readers"][r].get("value")]
        return ", ".join(said) or "no reader found it"

    unresolved = []
    if invoice_vote["of"] and not invoice_vote["confident"]:
        unresolved.append(f"invoice number ({describe(invoice_vote)})")
    for n, line in enumerate(line_votes, start=1):
        for kind in ("account", "amount"):
            if not line[kind]["confident"]:
                unresolved.append(f"GL line {n} {kind} ({describe(line[kind])})")
    second["unresolved"] = unresolved

    # What two readers agreed on goes into the main read. A value they did not
    # agree on keeps Flash's reading there -- the document stops anyway.
    if invoice_vote["confident"] and invoice_vote["final"] != flash_invoice:
        idents = [
            i for i in (doc.get("identifiers") or [])
            if "invoice" not in str(i.get("label") or "").lower()
            or "date" in str(i.get("label") or "").lower()
        ]
        doc["identifiers"] = [{"label": "Invoice Number", "value": invoice_vote["final"]}, *idents]

    agreed_lines = [
        ln for ln in line_votes if ln["account"]["confident"] and ln["amount"]["confident"]
    ]
    if agreed_lines and len(agreed_lines) == len(line_votes):
        doc["gl_mappings"] = [
            {"gl_account": ln["account"]["final"], "amount": ln["amount"]["final"],
             "mapped_description": ln["label"] or None}
            for ln in agreed_lines
        ]
        # The sign is also read from the notes (ocr_helpers.gl_notes), so the
        # GL lines there are replaced too; every other note is kept.
        kept = [
            n for n in doc.get("handwritten_notes") or []
            if not any(ocr_helpers._NOTE_GL_LINE.match(x) for x in ocr_helpers.note_lines(n))
        ]
        doc["handwritten_notes"] = kept + [
            f"#{ln['account']['final']} {ln['amount']['final']}" for ln in agreed_lines
        ]
    return doc
