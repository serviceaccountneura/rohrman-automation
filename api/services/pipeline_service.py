"""Unified upload pipeline — OCR, then dispatch to the right Tekion flow.

One document goes in, one `documents` row tracks it the whole way, and the
upload folder decides which Tekion flow runs:

    SUBLET         -> Sublet PO   + pre-invoice   (api/routes/tekion.py)
    MISCELLANEOUS  -> Misc PO     + pre-invoice   (api/routes/tekion.py)
    STOCK          -> Vendor stock order          (api/routes/tekion.py)
    OEM            -> Journal entry, saved as draft (api/services/je_creation.py)
    VEHICLE_MANUFACTURING -> Vehicle purchase journal entry from a
                      per-manufacturer template (api/services/vmi_je_creation.py)

None of those flows are reimplemented here — this module calls the existing
functions unchanged.

THE FOLDER IS AUTHORITATIVE
    The folder the user uploaded into selects the pipeline, always. OCR's own
    `document_type` is recorded as `ocr_document_type` so a disagreement is
    visible for review, but it never changes routing and never blocks.

RUNS AS A QUEUE JOB
    `run_job()` is called by the workers in api/services/worker.py, which claim
    rows from `documents`. Failures either requeue with backoff (transient) or
    park as an EXCEPTION (needs a human) — see `_RETRYABLE`.

CONCURRENCY
    The Tekion phase runs inside TEKION_LOCK because the Tekion client is a
    shared singleton whose dealership is mutable state. OCR runs outside the
    lock, so multiple documents are read in parallel while Tekion is talked to
    one at a time. See api/services/tekion_lock.py.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import Session

from api.db import engine
from api.models.db import Document
from api.services import (
    document_splitter,
    job_queue,
    ocr_helpers,
    po_reuse,
    s3_service,
)
from api.services.ocr_service import extract_document
from api.services.tekion_lock import dealer_scope, tekion_scope

# ── Folders the frontend can upload into ─────────────────────────────────────

FOLDER_SUBLET = "SUBLET"
FOLDER_MISC = "MISCELLANEOUS"
FOLDER_STOCK = "STOCK"
FOLDER_OEM = "OEM"
# Vehicle manufacturer invoices (Kia, Ford, Honda, Toyota). A journal entry
# like OEM, but built from a per-manufacturer template rather than the invoice
# total -- one car produces seven lines across inventory, notes payable and
# receivables. See api/services/vmi_je_creation.py.
FOLDER_VMI = "VEHICLE_MANUFACTURING"

VALID_FOLDERS = {FOLDER_SUBLET, FOLDER_MISC, FOLDER_STOCK, FOLDER_OEM, FOLDER_VMI}

# Aliases so the frontend can send the friendlier folder names.
_FOLDER_ALIASES = {
    "MISC": FOLDER_MISC,
    "MISCELLANEOUS": FOLDER_MISC,
    "SUBLET": FOLDER_SUBLET,
    "STOCK": FOLDER_STOCK,
    "VENDOR_STOCK": FOLDER_STOCK,
    "OEM": FOLDER_OEM,
    "PARTS_STMT": FOLDER_OEM,
    "MANUFACTURER": FOLDER_OEM,
    "VEHICLE_MANUFACTURING": FOLDER_VMI,
    "VEHICLE_MANUFACTURER": FOLDER_VMI,
    "VMI": FOLDER_VMI,
}


def normalize_folder(folder: str) -> str:
    """Map a frontend folder name to a pipeline key. Raises on unknown folders."""
    key = " ".join(str(folder or "").upper().split()).replace(" ", "_")
    resolved = _FOLDER_ALIASES.get(key)
    if not resolved:
        raise ValueError(
            f"Unknown folder {folder!r}. Expected one of: {', '.join(sorted(VALID_FOLDERS))}"
        )
    return resolved


# ── Exception vocabulary ─────────────────────────────────────────────────────

EX_OCR_FAILED = "OCR_FAILED"
EX_FILE_MISSING = "SOURCE_FILE_MISSING"
EX_MISSING_FIELD = "MISSING_REQUIRED_FIELD"
EX_DUPLICATE = "DUPLICATE_DOCUMENT"
EX_VENDOR_NOT_FOUND = "VENDOR_NOT_FOUND"
# Vendor stock orders are invoiced against an existing PO. If the number on the
# invoice does not resolve, there is nothing to attach to.
EX_PO_NOT_FOUND = "PO_NOT_FOUND"
# The PO named on the invoice exists but cannot take it. The specific code
# comes from po_reuse.blocking() -- PO_ALREADY_INVOICED or PO_CLOSED -- so
# the message a person reads names the actual cause.
EX_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
EX_UNBALANCED = "UNBALANCED_ENTRY"
# The parts read off an invoice do not add up to its total. Either OCR misread
# the page or the invoice itself does not add up -- either way a person has to
# look, so nothing is posted.
EX_LINE_ITEMS_MISMATCH = "LINE_ITEMS_MISMATCH"
# Nothing readable to itemise. A journal entry lists one line per part, so it
# cannot be built from the invoice total alone.
EX_NO_LINE_ITEMS = "NO_LINE_ITEMS"
# The vehicle flow refused to build an entry: no template for the manufacturer,
# or an amount the template needs was never written on the invoice. Not an
# error -- a document that needs a human step before it can be processed.
EX_VMI_REFUSED = "VEHICLE_ENTRY_REFUSED"
EX_TEKION_ERROR = "TEKION_ERROR"
# Tekion answered, and the answer was no. Distinct from TEKION_ERROR because a
# rejection is final — retrying re-runs OCR and asks the same question again.
EX_TEKION_REJECTED = "TEKION_REJECTED"

_SEVERITY = {
    EX_OCR_FAILED: "HIGH",
    EX_FILE_MISSING: "HIGH",
    EX_MISSING_FIELD: "HIGH",
    EX_DUPLICATE: "LOW",
    EX_VENDOR_NOT_FOUND: "HIGH",
    EX_PO_NOT_FOUND: "HIGH",
    EX_AMOUNT_MISMATCH: "HIGH",
    EX_UNBALANCED: "HIGH",
    EX_LINE_ITEMS_MISMATCH: "HIGH",
    EX_NO_LINE_ITEMS: "HIGH",
    EX_TEKION_ERROR: "HIGH",
    EX_TEKION_REJECTED: "HIGH",
}

# Only OCR is retried, and only because it is free of side effects: reading a
# document twice costs a Gemini call and changes nothing.
#
# Tekion failures are deliberately NOT retried. A run can fail *after* Tekion
# has already created a purchase order — that is exactly what happened when a
# log line raised mid-flow — and re-running the job creates a second PO in the
# customer's books. There is no way from here to tell how far a failed attempt
# got, so the safe assumption is that it may have written something. A human
# retries once they have checked Tekion.
_RETRYABLE = {EX_OCR_FAILED}


def _fail(session: Session, doc: Document, exception_type: str, error: str = "") -> None:
    job_queue.fail(
        session,
        doc,
        exception_type,
        severity=_SEVERITY.get(exception_type, "MEDIUM"),
        error=error,
        retryable=exception_type in _RETRYABLE,
    )


def _cleanup(doc: Document) -> None:
    """Delete the working file — only once the job can no longer be retried."""
    if doc.status not in (job_queue.STATUS_PROCESSED, job_queue.STATUS_EXCEPTION):
        return
    if not doc.source_path:
        return
    try:
        Path(doc.source_path).unlink(missing_ok=True)
    except OSError:
        pass


# ── Entry point (called by the workers) ───────────────────────────────────────


def run_job(document_id: UUID) -> None:
    """Process one claimed document. Always leaves it in a terminal or requeued state."""
    with Session(engine) as session:
        doc = session.get(Document, document_id)
        if doc is None:
            print(f"[PIPE] document {document_id} vanished before processing")
            return
        try:
            _run(doc, session)
        except Exception as e:  # noqa: BLE001 — the row must never be left PROCESSING
            print(f"[PIPE] {document_id} crashed: {e}")
            _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        finally:
            _cleanup(doc)


def _resolve_source(doc: Document) -> str | None:
    """The local path to the document, restoring it from S3 if the temp file is gone."""
    if doc.source_path and Path(doc.source_path).exists():
        return doc.source_path

    # A restart can wipe the temp file while the row is still queued.
    if doc.s3_key and s3_service.is_configured():
        try:
            suffix = Path(doc.file_name).suffix or ".pdf"
            restored = Path(doc.source_path) if doc.source_path else None
            target = str(restored) if restored else f"{doc.id}{suffix}"
            s3_service.download_file(doc.s3_key, target)
            print(f"[PIPE] {doc.id} restored source from S3")
            return target
        except Exception as e:  # noqa: BLE001
            print(f"[PIPE] {doc.id} could not restore from S3: {e}")
    return None



def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _split_batch(doc: Document, source: str, session: Session) -> bool:
    """Break a batch scan into one child document per invoice.

    Returns True when the document was split and this run is finished — the
    children are queued and the workers pick them up as ordinary jobs.

    Splitting wrongly would post invented invoices to Tekion, so every doubt
    resolves to "not a batch": a single segment, an unreadable file, or a
    segmentation the splitter would not vouch for all fall through to the
    normal single-document path.
    """
    if document_splitter.page_count(source) < 2:
        return False

    try:
        segments = document_splitter.segment_documents(source)
    except Exception as e:  # noqa: BLE001 — never fail a job over segmentation
        print(f"[SPLIT] {doc.id} segmentation error ({e}); processing as one document")
        return False

    if len(segments) < 2:
        return False

    print(f"[SPLIT] {doc.id} holds {len(segments)} invoices:")
    for seg in segments:
        print(f"[SPLIT]   {seg}")

    try:
        written = document_splitter.split_pdf(source, segments)
    except Exception as e:  # noqa: BLE001
        print(f"[SPLIT] {doc.id} could not be split ({e}); processing as one document")
        return False

    for index, (seg, path, digest) in enumerate(written, start=1):
        child_name = document_splitter.child_file_name(doc.file_name, seg, index)

        # Each child gets its OWN archived copy. Inheriting the parent's key
        # was wrong in a way that only shows up when someone opens one: the
        # preview served the whole batch, so a document covering page 3 looked
        # like it contained every invoice in the scan.
        #
        # A failure here is not fatal. The child still has its own file on
        # local disk, which is what processing uses; only the preview is lost.
        child_key = ""
        if s3_service.is_configured():
            try:
                child_key = s3_service.build_s3_key(child_name, doc.dealership_name)
                s3_service.upload_file(path, child_key)
            except Exception as e:  # noqa: BLE001
                print(f"[SPLIT] {doc.id} could not archive {child_name}: {e}")
                child_key = ""

        session.add(
            Document(
                file_name=child_name,
                s3_key=child_key,
                source_path=path,
                file_hash=digest,
                dealership_name=doc.dealership_name,
                # The folder decides the flow, so children inherit it — the
                # batch was dropped into one folder deliberately.
                po_type=doc.po_type,
                status=job_queue.STATUS_QUEUED,
                split_from=doc.id,
                page_range=(
                    str(seg.page_start)
                    if seg.page_count == 1
                    else f"{seg.page_start}-{seg.page_end}"
                ),
            )
        )

    # The parent is a container, not work. Terminal, so nothing reclaims it.
    doc.status = job_queue.STATUS_SPLIT
    doc.processed_at = _utcnow()
    doc.locked_by = ""
    doc.next_attempt_at = None
    session.add(doc)
    session.commit()
    print(f"[SPLIT] {doc.id} -> SPLIT into {len(written)} queued documents")
    return True


# ── OCR cache ────────────────────────────────────────────────────────────────
#
# The OCR result is written to disk for every document. Two reasons, and the
# second is why it is a cache and not just a log:
#
#   * every extraction bug in the vehicle flow has come from guessing at a
#     structure Gemini chose rather than reading it, and
#   * a document re-run with corrected fields does not need reading again. The
#     invoice has not changed, the upload's temp file is usually gone by then,
#     and a second Gemini pass costs money to return the same answer.


def _ocr_cache_path(doc: Document) -> Path:
    return Path(tempfile.gettempdir()) / "rohrman" / "ocr" / f"{doc.id}.json"


def _cache_ocr(doc: Document, ocr: dict[str, Any]) -> None:
    try:
        path = _ocr_cache_path(doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ocr, indent=2, default=str), encoding="utf-8")
        print(f"[PIPE] {doc.id} ocr cached at {path}")
    except Exception as e:  # noqa: BLE001
        # Never the reason a document fails: this is diagnostics and a
        # convenience, and the document can always be read again.
        print(f"[PIPE] {doc.id} could not cache OCR: {e}")


def _load_cached_ocr(doc: Document) -> dict[str, Any] | None:
    try:
        path = _ocr_cache_path(doc)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} could not read cached OCR: {e}")
        return None


def manual_overrides(doc: Document) -> dict[str, Any]:
    """What a person typed in for this document, or {}."""
    if not doc.manual_fields:
        return {}
    try:
        parsed = json.loads(doc.manual_fields)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        print(f"[PIPE] {doc.id} manual_fields is not valid JSON; ignoring")
        return {}


def _run(doc: Document, session: Session) -> None:
    # A re-run with corrected fields reuses the OCR from the first attempt. The
    # invoice has not changed, and by this point the uploaded temp file has
    # usually been cleaned up -- so insisting on reading it again would make
    # "fix the stock number and try again" impossible for exactly the documents
    # that need it.
    overrides = manual_overrides(doc)
    cached = _load_cached_ocr(doc) if overrides else None

    # ── 1. Locate the file ───────────────────────────────────────────────────
    source = _resolve_source(doc)
    if not source and cached is None:
        _fail(session, doc, EX_FILE_MISSING, error=f"no readable source for {doc.file_name!r}")
        return

    # ── 1b. Split a batch scan before OCR ────────────────────────────────────
    # OCR describes one document, so several invoices in one file have to become
    # several documents first. Children are never re-segmented.
    if cached is None and not doc.split_from and _split_batch(doc, source, session):
        return

    # ── 2. OCR ───────────────────────────────────────────────────────────────
    if cached is not None:
        print(f"[PIPE] {doc.id} re-run: reusing cached OCR, overrides={overrides}")
        ocr = cached
    else:
        print(f"[PIPE] {doc.id} OCR starting ({doc.po_type} folder)")
        try:
            ocr = extract_document(source)
        except Exception as e:  # noqa: BLE001
            print(f"[PIPE] OCR failed: {e}")
            _fail(session, doc, EX_OCR_FAILED, error=str(e))
            return
        _cache_ocr(doc, ocr)

    # ── 3. Record what OCR found ─────────────────────────────────────────────
    doc.ocr_document_type = ocr_helpers.get_document_type(ocr)
    doc.vendor_name = ocr_helpers.get_vendor_name(ocr) or doc.vendor_name
    doc.invoice_number = ocr_helpers.get_invoice_number(ocr)
    doc.ro_number = ocr_helpers.get_control_number(ocr)
    doc.vin = ocr_helpers.get_vin(ocr)
    if not doc.dealership_name:
        doc.dealership_name = ocr_helpers.get_dealership_name(ocr)
    session.add(doc)
    session.commit()

    if doc.ocr_document_type:
        print(f"[PIPE] {doc.id} OCR type={doc.ocr_document_type!r}, folder={doc.po_type} "
              f"(folder wins)")

    # ── 4. Idempotency ───────────────────────────────────────────────────────
    # Checked after OCR because the invoice number is only known now. A repeat
    # upload is held for a decision rather than posted twice to Tekion — see
    # job_queue.hold_as_duplicate. A user who confirms gets `duplicate_override`
    # set for exactly this run.
    if doc.duplicate_override:
        doc.duplicate_override = False
        session.add(doc)
        session.commit()
        print(f"[PIPE] {doc.id} duplicate check waived by confirmation")
    else:
        duplicate = job_queue.find_duplicate(session, doc)
        if duplicate is not None:
            job_queue.hold_as_duplicate(session, doc, duplicate)
            return

    # ── 4b. Deleted while it was running ─────────────────────────────────────
    # OCR takes tens of seconds, so there is a real window between claiming a
    # document and posting it. Someone deleting it in that window means stop --
    # the claim check cannot help, because the row was already claimed.
    session.refresh(doc)
    if doc.deleted_at is not None:
        print(f"[PIPE] {doc.id} deleted while processing -- nothing posted")
        return

    # ── 5. Dispatch on the folder ────────────────────────────────────────────
    if doc.po_type == FOLDER_VMI:
        _run_vehicle_journal_entry(doc, ocr, session)
    elif doc.po_type == FOLDER_OEM:
        _run_journal_entry(doc, ocr, session)
    elif doc.po_type == FOLDER_STOCK:
        # A vendor stock order is not created here — the PO already exists and
        # the invoice arrives afterwards to be attached to it.
        _run_stock_pre_invoice(doc, ocr, session, source)
    else:
        # `source` is handed on so the PO flow can attach the invoice PDF to the
        # pre-invoice — we already have the file, so there is no reason not to.
        _run_purchase_order(doc, ocr, session, source)


# ── OEM -> Journal entry ──────────────────────────────────────────────────────


def _record_postings(
    doc: Document,
    session: Session,
    *,
    lines: list[dict[str, Any]],
    reference: str = "",
    balance: float | None = None,
) -> None:
    """Record the accounting lines this document put into Tekion.

    One shape for all five flows, so a single component can render any of them:

        {"lines": [{glAccount, glName, amount, control, source}],
         "creditTotal", "debitTotal", "balance", "reference"}

    Credits and debits are derived from the SIGN rather than taken on trust.
    Every flow already carries a credit as a negative amount -- that is what
    Tekion is sent -- so totalling the signs here cannot disagree with what was
    posted, whereas a separate pair of totals could.

    Written on failures too. A refused entry is exactly when someone wants to
    see which lines were built and where they stopped adding up.
    """
    clean = [
        {
            "glAccount": str(line.get("glAccount") or ""),
            "glName": str(line.get("glName") or ""),
            "amount": round(float(line.get("amount") or 0), 2),
            "control": str(line.get("control") or ""),
            "source": str(line.get("source") or ""),
        }
        for line in lines
        if line.get("glAccount")
    ]
    credit = round(sum(-l["amount"] for l in clean if l["amount"] < 0), 2)
    debit = round(sum(l["amount"] for l in clean if l["amount"] > 0), 2)

    doc.posting_details = json.dumps(
        {
            "lines": clean,
            "creditTotal": credit,
            "debitTotal": debit,
            "balance": round(debit - credit, 2) if balance is None else round(balance, 2),
            "reference": reference,
        },
        default=str,
    )[:8000]
    session.add(doc)
    session.commit()


def _run_journal_entry(doc: Document, ocr: dict[str, Any], session: Session) -> None:
    """Parts Manufacture Ticket -> journal entry, saved as a draft."""
    from api.routes.tekion import get_client, reset_client
    from api.services.je_creation import ExpectedJournalEntry, create_journal_entry

    invoice_number = doc.invoice_number
    invoice_date = ocr_helpers.get_invoice_date(ocr)
    invoice_amount = ocr_helpers.get_total_amount(ocr)

    # The SOP needs exactly these three off the ticket.
    missing = [
        name
        for name, value in (
            ("invoice_number", invoice_number),
            ("invoice_date", invoice_date),
            ("invoice_amount", invoice_amount),
        )
        if not value
    ]
    if missing:
        _fail(session, doc, EX_MISSING_FIELD, error=f"missing: {', '.join(missing)}")
        return

    expected = ExpectedJournalEntry(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        invoice_amount=invoice_amount,
        dealership_name=doc.dealership_name,
        # Each part becomes its own debit line on the entry.
        line_items=ocr_helpers.get_raw_line_items(ocr),
        # A GL account written on the invoice outranks anything we infer.
        invoice_gl_account=ocr_helpers.get_document_gl_account(ocr),
        # Accounts with their own amounts. One debit line each, exactly as
        # written -- see build_postings.
        gl_splits=ocr_helpers.get_gl_amount_splits(ocr),
    )

    try:
        # Serialized: create_journal_entry switches dealership on the shared client.
        with tekion_scope():
            client = get_client(session)
            result = create_journal_entry(client, expected=expected, dry_run=False)
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} journal entry failed: {e}")
        reset_client()
        _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        return

    _record_postings(
        doc,
        session,
        lines=[
            {
                "glAccount": p.get("_glAccountNumber"),
                "glName": p.get("_glAccountName") or p.get("refText"),
                "amount": p.get("amount"),
                "control": result.control_number or "",
                "source": result.debit_gl_source or "",
            }
            for p in result.postings
        ],
        reference=result.transaction_number or result.reference or "",
        balance=result.balance,
    )

    # Checked before `balanced`: a refused entry never got as far as balancing,
    # so "balance $0.00" would be a confusing thing to show someone.
    if result.line_items_mismatch:
        _fail(
            session,
            doc,
            EX_NO_LINE_ITEMS if result.no_line_items else EX_LINE_ITEMS_MISMATCH,
            error="; ".join(result.notes) or "invoice parts do not match the total",
        )
        return
    if not result.balanced:
        _fail(session, doc, EX_UNBALANCED, error=f"balance ${result.balance:.2f}")
        return
    if not result.saved:
        _fail(session, doc, EX_TEKION_ERROR, error="draft was not saved")
        return

    doc.transaction_id = result.transaction_id or ""
    doc.transaction_number = result.transaction_number or ""
    doc.journal_id = result.journal_id or ""
    job_queue.complete(session, doc)
    print(f"[PIPE] {doc.id} -> PROCESSED (JE {doc.transaction_number}, {result.status})")


# ── VEHICLE_MANUFACTURING -> Auto Posting journal entry ──────────────────────


def _run_vehicle_journal_entry(
    doc: Document, ocr: dict[str, Any], session: Session
) -> None:
    """Vehicle manufacturer invoice -> journal entry from a template.

    Unlike the OEM flow this does not derive the entry from the invoice total.
    The manufacturer decides the shape of the entry, and several of its amounts
    are only on the page because a clerk wrote them there. When one is missing
    the document is failed rather than approximated -- see vmi_je_creation.
    """
    from api.routes.tekion import get_client, reset_client
    from api.services import vmi_helpers
    from api.services.vmi_je_creation import create_vehicle_journal_entry


    facts = vmi_helpers.build_facts(ocr, doc.dealership_name)
    vmi_helpers.apply_overrides(facts, manual_overrides(doc))

    # Only the accounting date is genuinely required. A vehicle entry is keyed on
    # the STOCK NUMBER -- that is what goes in refId, refText and the
    # description -- and invoice_number is not used to build it at all.
    #
    # Ford proves the point: its invoices carry no invoice number. The field is
    # "Invoice & Unit Identification NO." and its value is the VIN. Demanding a
    # number that does not exist rejected a document the flow could process
    # perfectly well. The stock number is checked later, where it is used.
    if not facts.invoice_date:
        # Recorded the same way a flow refusal is, so the correction form knows
        # to ask for a date even though this never reached the vehicle flow.
        doc.vehicle_details = json.dumps({"needs": ["invoice_date"]})
        session.add(doc)
        _fail(session, doc, EX_MISSING_FIELD, error="missing: invoice_date")
        return

    print(
        f"[PIPE] {doc.id} vehicle invoice: {facts.manufacturer or '(unknown make)'} "
        f"stock={facts.stock_number or '-'} vin={facts.vin or '-'} "
        f"cost={facts.dealer_cost_total:.2f} annotations={facts.gl_annotations}"
    )
    # The raw OCR fields this flow depends on. Printed unconditionally because
    # when nothing is annotated the only useful question is what the model
    # actually saw.
    print(f"[PIPE] {doc.id} ocr.gl_mappings={ocr.get('gl_mappings')}")
    print(f"[PIPE] {doc.id} ocr.handwritten_notes={ocr.get('handwritten_notes')}")


    try:
        # dealer_scope, not tekion_scope: it switches dealership INSIDE the lock,
        # so no other job can retarget the shared client between the switch and
        # the calls that follow. Using the bare lock here read another store's
        # templates entirely -- see vmi_je_creation's dealer note.
        with dealer_scope(get_client(session), doc.dealership_name):
            client = get_client(session)
            result = create_vehicle_journal_entry(client, facts, dry_run=False)
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} vehicle journal entry failed: {e}")
        reset_client()
        _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        return

    # Recorded on EVERY outcome, before any of the branches below return. A
    # refused document is the one someone opens, and until now the detail page
    # had nothing to show them beyond the error string.
    doc.vehicle_details = json.dumps(
        {
            "manufacturer": facts.manufacturer,
            "stockNumber": facts.stock_number,
            "vin": facts.vin,
            "invoiceDate": facts.invoice_date,
            "dealerCostTotal": facts.dealer_cost_total,
            "msrpTotal": facts.msrp_total,
            "glAnnotations": facts.gl_annotations,
            "templateName": result.tekion_template_name,
            "creditTotal": result.credit_total,
            "debitTotal": result.debit_total,
            "balance": result.balance,
            "refusal": result.refusal,
            # Which fields would fix it. Empty means nothing a person can type
            # will -- the problem is Tekion configuration.
            "needs": result.needs,
            "postings": [
                {
                    "glAccount": p.get("_glAccountNumber"),
                    "glName": p.get("description"),
                    "amount": p.get("amount"),
                    "control": p.get("_control"),
                    "source": p.get("_source"),
                }
                for p in result.postings
            ],
        },
        default=str,
    )[:8000]
    session.add(doc)
    session.commit()

    _record_postings(
        doc,
        session,
        lines=[
            {
                "glAccount": p.get("_glAccountNumber"),
                "glName": p.get("description"),
                "amount": p.get("amount"),
                "control": p.get("_control"),
                "source": p.get("_source"),
            }
            for p in result.postings
        ],
        reference=result.transaction_number or facts.stock_number or "",
        balance=result.balance,
    )

    # Checked before the balance: a refused entry never reached the balance
    # check, so reporting "balance $0.00" would be misleading.
    if result.refusal:
        _fail(session, doc, EX_VMI_REFUSED, error=result.refusal)
        return
    if result.problems:
        _fail(
            session,
            doc,
            EX_VMI_REFUSED,
            error="; ".join(str(p) for p in result.problems),
        )
        return
    if not result.balanced:
        _fail(session, doc, EX_UNBALANCED, error=f"balance ${result.balance:.2f}")
        return
    if not result.saved:
        _fail(session, doc, EX_TEKION_ERROR, error="draft was not saved")
        return

    doc.transaction_id = result.transaction_id or ""
    doc.transaction_number = result.transaction_number or ""
    doc.journal_id = result.journal_id or ""
    job_queue.complete(session, doc)
    print(
        f"[PIPE] {doc.id} -> PROCESSED "
        f"(vehicle JE {doc.transaction_number}, {len(result.postings)} lines)"
    )


# ── STOCK -> pre-invoice an existing vendor stock order ──────────────────────


def _run_stock_pre_invoice(
    doc: Document,
    ocr: dict[str, Any],
    session: Session,
    source_path: str | None = None,
) -> None:
    """Attach the invoice to the purchase order it names, and pre-invoice it."""
    from api.routes.tekion import get_client, reset_client
    from api.services.vso_po_creation import (
        ExpectedStockInvoice,
        pre_invoice_stock_order,
    )

    po_number = ocr_helpers.get_po_number(ocr)
    invoice_number = doc.invoice_number
    total = ocr_helpers.get_total_amount(ocr)
    sales_tax = ocr_helpers.get_sales_tax(ocr)

    # The PO number is what makes this flow possible at all.
    if not po_number:
        _fail(
            session,
            doc,
            EX_PO_NOT_FOUND,
            error="No purchase order number could be read from the invoice",
        )
        return
    if not invoice_number or not total:
        _fail(session, doc, EX_MISSING_FIELD, error="missing invoice number or total")
        return

    # Record it now so the row shows which PO this is about, even if the run
    # fails later.
    doc.po_number = po_number
    session.add(doc)
    session.commit()

    expected = ExpectedStockInvoice(
        po_number=po_number,
        invoice_number=invoice_number,
        invoice_amount=total,
        sales_tax=sales_tax,
        dealership_name=doc.dealership_name,
        invoice_date=ocr_helpers.get_invoice_date(ocr) or None,
        invoice_file_path=source_path,
        invoice_file_name=doc.file_name or None,
        # Only consulted if Tekion has no GL accounts of its own for these parts.
        gl_account=ocr_helpers.get_document_gl_account(ocr),
        # Accounts with their own amounts. These outrank Tekion's postings.
        gl_splits=ocr_helpers.get_gl_amount_splits(ocr),
    )

    try:
        with tekion_scope():
            client = get_client(session)
            result = pre_invoice_stock_order(client, expected, dry_run=False)
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} stock pre-invoice failed: {e}")
        reset_client()
        _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        return

    if not result.po_id:
        _fail(
            session,
            doc,
            EX_PO_NOT_FOUND,
            error="; ".join(result.notes) or f"PO {po_number} not found",
        )
        return
    if result.discrepancies:
        _fail(
            session,
            doc,
            EX_AMOUNT_MISMATCH,
            error="; ".join(str(d) for d in result.discrepancies),
        )
        return
    if not result.posted:
        _fail(session, doc, EX_TEKION_ERROR, error="; ".join(result.notes) or "not posted")
        return

    doc.po_number = result.po_number or po_number
    doc.vendor_name = result.vendor_name or doc.vendor_name

    # A stock order's expense lines are Tekion's own -- one per part, each with
    # the account that part belongs to -- so the split is not ours to describe.
    # What IS ours is the net it was invoiced for against the A/P credit, which
    # is the pair a person checks.
    _record_postings(
        doc,
        session,
        lines=[
            {
                "glAccount": "(per part)",
                "glName": "Tekion's own postings, one per part on the order",
                "amount": round(result.net_amount or 0, 2),
                "source": "returned by Tekion",
            },
            {
                "glAccount": "3002",
                "glName": "A/P",
                "amount": -round(total or 0, 2),
                "source": "accounts payable",
            },
        ],
        reference=result.po_number or po_number,
    )

    job_queue.complete(session, doc)
    print(f"[PIPE] {doc.id} -> PROCESSED (pre-invoiced PO {doc.po_number})")


# ── SUBLET / MISCELLANEOUS -> Purchase order ─────────────────────────────────


def _resolve_existing_po(
    doc: Document,
    ocr: dict[str, Any],
    session: Session,
) -> tuple[dict[str, Any] | None, bool]:
    """Decide whether this invoice should reuse a purchase order already in Tekion.

    Returns (existing_po, stop). `existing_po` is a PO to invoice instead of
    creating one; `stop` means this run is over -- the document has been parked
    for a decision or failed -- and the caller must return without posting.

    THE ORDER OF THE QUESTIONS, and why each one exits early:

      1. Is a PO number written on the invoice? No number means the invoice is
         not referring to an existing order at all, and the normal flow is
         simply right. This is the common case and costs one regex.

      2. Did a person already answer for this run? "Create a new one" means
         they looked and said no; honour it without asking twice. The answer is
         consumed here, so a later upload is asked again.

      3. Does that PO exist at THIS dealership? Only an exact match counts --
         Tekion's search is fuzzy, and a near-miss must not be treated as the
         PO the clerk meant.

      4. Can it take this invoice? A cancelled PO cannot, and a PO already
         carrying this invoice number must not. The second is not covered by
         the duplicate check upstream: that catches a repeat of a document WE
         processed, while this catches a PO invoiced in Tekion by hand, which
         nothing in our own records would show.

      5. Otherwise, ask. Nothing here decides on its own to post against an
         order somebody else raised.
    """
    from api.routes.tekion import _resolve_dealer, get_client

    if doc.po_type not in (FOLDER_SUBLET, FOLDER_MISC):
        return None, False

    po_number = ocr_helpers.get_po_number(ocr)
    if not po_number:
        return None, False

    choice = (doc.po_choice or "").upper()
    if choice:
        # Consumed whichever way it went, so it applies to this attempt only.
        doc.po_choice = ""
        session.add(doc)
        session.commit()

    if choice == po_reuse.CHOICE_NEW:
        print(f"[PIPE] {doc.id} PO {po_number} exists but a new one was requested")
        return None, False

    # The lookup is per-dealership, so the switch has to happen first.
    with tekion_scope():
        client = get_client(session)
        _resolve_dealer(client, doc.dealership_name)
        found = po_reuse.look_up(client, po_number)

    if found is None:
        print(f"[PIPE] {doc.id} PO {po_number!r} is not in Tekion -- creating a new one")
        return None, False

    doc.po_number = found.po_number or po_number
    session.add(doc)
    session.commit()

    blocked_code, blocked = po_reuse.blocking(found, doc.invoice_number)

    if choice == po_reuse.CHOICE_EXISTING:
        if blocked:
            # The PO changed between being offered and being chosen, or someone
            # invoiced it in the meantime. Refusing beats posting anyway.
            _fail(session, doc, blocked_code, error=blocked)
            return None, True
        print(f"[PIPE] {doc.id} reusing {po_reuse.describe(found)}")
        return found.raw, False

    if blocked:
        # Nothing to ask: neither answer would help. A new PO is not the fix for
        # an invoice that has already been posted against this one.
        _fail(session, doc, blocked_code, error=blocked)
        return None, True

    job_queue.hold_for_po_decision(
        session,
        doc,
        found,
        # The card beside this spells out the choice; the row only needs to say
        # which PO it is about.
        po_reuse.describe(found),
    )
    return None, True


def _run_purchase_order(
    doc: Document,
    ocr: dict[str, Any],
    session: Session,
    source_path: str | None = None,
) -> None:
    """Build the typed PO request from OCR and hand it to the existing flow.

    `source_path` is the invoice file. The PO flow uploads it to Tekion's media
    service and attaches it to the pre-invoice, which is the same thing a clerk
    does by hand after creating the PO.
    """
    from fastapi import HTTPException

    from api.models.schemas import (
        CreateMiscPoRequest,
        GlSplitInput,
        CreateStockPoRequest,
        CreateSubletPoRequest,
        MiscLineItem,
        StockPartInput,
        SubletLineItem,
    )
    from api.routes.tekion import _create_misc_po, _create_stock_po, _create_sublet_po, _resolve_dealer, get_client
    from api.services.job_matching import match_line_items_to_jobs

    total = ocr_helpers.get_total_amount(ocr)
    sales_tax = ocr_helpers.get_sales_tax(ocr)
    line_items = ocr_helpers.get_raw_line_items(ocr)

    if not doc.vendor_name or not total:
        _fail(session, doc, EX_MISSING_FIELD, error="missing vendor name or total amount")
        return

    # The purchase order must be worth exactly what the pre-invoice posts
    # against it. The PO is built from OCR's line items while the pre-invoice
    # uses the grand total less tax, and Tekion rejects the invoice outright
    # ("unable to update PO") when the two disagree — after the PO has already
    # been created, leaving an orphan.
    #
    # OCR line items are the unreliable half: a missed row or an unreadable
    # unit price silently shrinks the PO. When they do not reconcile, fall back
    # to a single line for the correct amount — less itemised, but truthful
    # about what is owed, and it matches what the existing flow already does
    # when OCR finds no line items at all.
    # The rows AS READ, before the reconciliation below is allowed to discard
    # them. A sublet invoice's repair order numbers live on these rows, and
    # collapsing to a single line -- which is the right answer for the PO total
    # -- would throw them away and leave the document reporting that it has no
    # repair orders, when it plainly has eight written on it.
    rows_as_read = list(line_items)

    expected_po_total = round(total - sales_tax, 2)
    line_total = round(sum(i["qty"] * i["unitPrice"] for i in line_items), 2)
    if line_items and abs(line_total - expected_po_total) > 0.01:
        print(
            f"[PIPE] {doc.id} line items total {line_total} but invoice is "
            f"{expected_po_total} (net of {sales_tax} tax) — using a single line"
        )
        line_items = []
    elif line_items:
        print(f"[PIPE] {doc.id} line items reconcile to {line_total}")

    common = {
        "dealership_name": doc.dealership_name,
        "vendor_name": doc.vendor_name,
        "invoice_number": doc.invoice_number,
        "invoice_amount": total,
        "sales_tax": sales_tax,
        # Attached to the pre-invoice by the PO flow. Stock orders have no
        # pre-invoice, so it is simply unused there.
        "invoice_file_path": source_path,
    }

    existing_po, stop = _resolve_existing_po(doc, ocr, session)
    if stop:
        return

    try:
        if doc.po_type == FOLDER_SUBLET:
            # Sublet invoices do not carry a trustworthy RO number, so the RO
            # is found by VIN instead: switch to the invoice's dealership,
            # search for repair orders on that VIN, and take the most recent
            # one that is still open. Each job on that RO is then matched
            # against the invoice's line-item descriptions by the LLM, using
            # the job's captured concern + tech story text — the OCR'd RO
            # number is no longer used at all.
            #
            # None of it applies to a PO somebody else already raised: that PO
            # carries its own RO and job, and searching for another would at
            # best find the same one and at worst fail the document over a step
            # its outcome does not depend on.
            if existing_po:
                req = CreateSubletPoRequest(**common, line_items=[])
                with tekion_scope():
                    response = _create_sublet_po(req, session, existing_po=existing_po)
                _finish_purchase_order(doc, response, session)
                return

            # THE INVOICE NAMES ITS OWN REPAIR ORDERS.
            #
            # The VIN search below exists because a sublet invoice usually bills
            # one car and does not say which repair order it was, so the RO has
            # to be found from the vehicle. Some vendors do say: a body shop
            # billing eight cars at once writes the RO beside each row, because
            # the invoice would be unreadable otherwise.
            #
            # When they are there, use them. They are better evidence than the
            # VIN search is -- the clerk wrote down the RO this work was done
            # on, rather than us inferring it from the most recent open RO on a
            # vehicle -- and a page listing eight cars has no single VIN for the
            # search to start from at all.
            # From the rows as read: see rows_as_read. A sublet invoice whose
            # prices failed to reconcile still names its repair orders, and the
            # orders are what this path needs.
            written_ros = [str(item.get("roNumber") or "") for item in rows_as_read]
            if rows_as_read and all(written_ros):
                with tekion_scope():
                    client = get_client(session)
                    _resolve_dealer(client, doc.dealership_name)
                    items, problem = _sublet_items_from_written_ros(
                        client, rows_as_read, written_ros
                    )
                if problem:
                    _fail(session, doc, EX_TEKION_REJECTED, error=problem)
                    return

                req = CreateSubletPoRequest(**common, line_items=items)
                with tekion_scope():
                    response = _create_sublet_po(req, session)
                _finish_purchase_order(doc, response, session)
                return

            if not doc.vin:
                _fail(
                    session,
                    doc,
                    EX_MISSING_FIELD,
                    error=(
                        "this sublet invoice has no VIN and no repair order "
                        "number on its rows, so there is nothing to attach the "
                        "work to"
                    ),
                )
                return

            with tekion_scope():
                client = get_client(session)
                _resolve_dealer(client, doc.dealership_name)
                ro = client.find_latest_open_ro_by_vin(doc.vin)
                if not ro:
                    _fail(session, doc, EX_TEKION_REJECTED, error=f"no open RO found for VIN {doc.vin}")
                    return
                jobs = client.get_ro_job_details(ro["id"])
                if not jobs:
                    _fail(session, doc, EX_TEKION_REJECTED, error=f"no jobs found on RO for VIN {doc.vin}")
                    return

                descriptions = [item["description"] or "Sublet repair" for item in line_items] or ["Sublet repair"]
                job_numbers = match_line_items_to_jobs(descriptions, jobs)
                ro_number = ro.get("roNo") or ""

                if line_items:
                    items = [
                        SubletLineItem(
                            ro_number=ro_number,
                            job_number=job_numbers[i],
                            description=item["description"] or "Sublet repair",
                            labor_amount=0.0,
                            parts_amount=item["totalPrice"] or item["unitPrice"],
                        )
                        for i, item in enumerate(line_items)
                    ]
                else:
                    items = [
                        SubletLineItem(
                            ro_number=ro_number,
                            job_number=job_numbers[0],
                            description="Sublet repair",
                            labor_amount=0.0,
                            parts_amount=expected_po_total,
                        )
                    ]

                req = CreateSubletPoRequest(**common, line_items=items)
                response = _create_sublet_po(req, session)

        elif doc.po_type == FOLDER_STOCK:
            req = CreateStockPoRequest(
                **common,
                parts=[
                    StockPartInput(
                        part_number=item["description"],
                        part_name=item["description"],
                        qty=item["qty"],
                        unit_price=item["unitPrice"],
                        brand_code="",
                    )
                    for item in line_items
                ],
            )
            with tekion_scope():
                response = _create_stock_po(req, session)

        else:  # MISCELLANEOUS
            misc_items = [
                MiscLineItem(
                    part_name=item["description"] or "Misc purchase",
                    qty=item["qty"],
                    unit_price=item["unitPrice"],
                    gl_account=item.get("glAccount") or None,
                )
                for item in line_items
            ] or [
                MiscLineItem(
                    part_name=f"Invoice {doc.invoice_number}" if doc.invoice_number else "Misc purchase",
                    qty=1.0,
                    unit_price=expected_po_total,
                )
            ]
            # Accounts and amounts written as a block outrank anything derived
            # from the rows -- see get_gl_amount_splits.
            req = CreateMiscPoRequest(
                **common,
                line_items=misc_items,
                gl_splits=[
                    GlSplitInput(
                        gl_account=sp["gl_account"],
                        amount=sp["amount"],
                        description=sp["description"],
                    )
                    for sp in ocr_helpers.get_gl_amount_splits(ocr)
                ],
            )
            with tekion_scope():
                response = _create_misc_po(req, session, existing_po=existing_po)

    except HTTPException as e:
        # An HTTPException here is Tekion (or our own validation) saying no:
        # a 422 for an unmapped vendor, a 404 for a missing RO. None of that
        # becomes true on a retry, so these are terminal — retrying would only
        # re-run OCR and get the same answer three times.
        detail = e.detail if isinstance(e.detail, dict) else {}
        reason = str(detail.get("reason", "") or e.detail)
        print(f"[PIPE] {doc.id} PO rejected: {reason}")
        _fail(
            session,
            doc,
            EX_VENDOR_NOT_FOUND if "mapping" in reason else EX_TEKION_REJECTED,
            error=reason,
        )
        return
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} PO creation failed: {e}")
        _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        return

    _finish_purchase_order(doc, response, session)


def _sublet_items_from_written_ros(
    client: Any,
    line_items: list[dict[str, Any]],
    written_ros: list[str],
) -> tuple[list[Any], str]:
    """Build sublet PO lines from the repair orders written on the invoice.

    Returns (items, problem). A non-empty problem means stop -- every line has
    to land on a real job, because a PO short one line bills the vendor for work
    that is recorded against nothing.

    Each row keeps its OWN repair order. A Tekion sublet line names a job, and
    one invoice can span eight vehicles and eight orders; they are not
    interchangeable and nothing here falls back to the first one.
    """
    from fastapi import HTTPException  # noqa: F401  (kept for symmetry)

    from api.models.schemas import SubletLineItem
    from api.services.job_matching import match_line_items_to_jobs

    items: list[Any] = []
    for item, ro_number in zip(line_items, written_ros):
        # Open orders first; then, failing that, any order at all.
        #
        # A vendor who bills weeks after the work writes down an order that has
        # long since closed -- every one of the eight on the G.A.R.I. invoice
        # had. Refusing to look turns a number the clerk wrote down into "no
        # such RO", which is simply untrue.
        found = client.search_ro(ro_number) or client.search_ro(
            ro_number, include_closed=True
        )
        if not found:
            return [], (
                f"there is no repair order {ro_number} at this dealership"
            )

        # An exact match, not merely the first hit: the lookup is a text search
        # and "1581221" also matches "15812210".
        ro = next(
            (r for r in found if str(r.get("roNumber")) == str(ro_number)), found[0]
        )
        jobs = client.get_ro_jobs(ro["id"])
        if not jobs:
            return [], f"repair order {ro_number} has no jobs to bill this against"

        description = item.get("description") or "Sublet repair"
        if len(jobs) == 1:
            # Nothing to choose between. Every order on the invoice that
            # prompted this had exactly one job, which is the usual shape when
            # a body shop is billing one repair per car.
            job_number = jobs[0]["jobNumber"]
        else:
            # Several jobs on one order, so the row has to say which. The same
            # LLM match the VIN path uses -- and it needs get_ro_job_details,
            # not get_ro_jobs: the latter returns no concern or story text at
            # all, so matching against it would be matching against nothing.
            detailed = client.get_ro_job_details(ro["id"]) or jobs
            job_number = match_line_items_to_jobs([description], detailed)[0]

        items.append(
            SubletLineItem(
                ro_number=ro["roNumber"] or ro_number,
                job_number=job_number,
                description=description,
                labor_amount=0.0,
                parts_amount=item.get("totalPrice") or item.get("unitPrice") or 0.0,
            )
        )

    return items, ""


def _finish_purchase_order(doc: Document, response: Any, session: Session) -> None:
    """Record the outcome of a PO run, however the PO was arrived at.

    Shared by the create path and the reuse path so the two cannot drift: a
    reused PO is PROCESSED on exactly the same terms as one we raised.
    """
    # What the pre-invoice put where. Recorded before the success check so a
    # partial run still shows the lines it built.
    if getattr(response, "gl_lines", None):
        _record_postings(
            doc,
            session,
            lines=[
                {
                    "glAccount": line.gl_account,
                    "glName": line.gl_name,
                    "amount": line.amount,
                    "control": doc.ro_number or "",
                    "source": line.source,
                }
                for line in response.gl_lines
            ],
            reference=response.po_number or doc.po_number or "",
        )

    if not response.success:
        _fail(session, doc, EX_TEKION_ERROR, error=response.error or "PO creation failed")
        return

    # `or doc.po_number` for the reuse path: the number was recorded when the
    # PO was found, and a response that omits it should not blank it.
    doc.po_number = response.po_number or doc.po_number or ""
    doc.vendor_name = response.vendor_name or doc.vendor_name
    job_queue.complete(session, doc)
    print(f"[PIPE] {doc.id} -> PROCESSED (PO {doc.po_number})")
