"""Unified upload pipeline — OCR, then dispatch to the right Tekion flow.

One document goes in, one `documents` row tracks it the whole way, and the
upload folder decides which Tekion flow runs:

    SUBLET         -> Sublet PO   + pre-invoice   (api/routes/tekion.py)
    MISCELLANEOUS  -> Misc PO     + pre-invoice   (api/routes/tekion.py)
    STOCK          -> Vendor stock order          (api/routes/tekion.py)
    OEM            -> Journal entry, saved as draft (api/services/je_creation.py)

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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import Session

from api.db import engine
from api.models.db import Document
from api.services import job_queue, ocr_helpers, s3_service
from api.services.ocr_service import extract_document
from api.services.tekion_lock import tekion_scope

# ── Folders the frontend can upload into ─────────────────────────────────────

FOLDER_SUBLET = "SUBLET"
FOLDER_MISC = "MISCELLANEOUS"
FOLDER_STOCK = "STOCK"
FOLDER_OEM = "OEM"

VALID_FOLDERS = {FOLDER_SUBLET, FOLDER_MISC, FOLDER_STOCK, FOLDER_OEM}

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
EX_UNBALANCED = "UNBALANCED_ENTRY"
EX_TEKION_ERROR = "TEKION_ERROR"

_SEVERITY = {
    EX_OCR_FAILED: "HIGH",
    EX_FILE_MISSING: "HIGH",
    EX_MISSING_FIELD: "HIGH",
    EX_DUPLICATE: "LOW",
    EX_VENDOR_NOT_FOUND: "HIGH",
    EX_UNBALANCED: "HIGH",
    EX_TEKION_ERROR: "HIGH",
}

# Only transient problems are worth retrying. A missing invoice number will not
# fix itself, so retrying it just burns attempts and delays the human seeing it.
_RETRYABLE = {EX_OCR_FAILED, EX_TEKION_ERROR}


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


def _run(doc: Document, session: Session) -> None:
    # ── 1. Locate the file ───────────────────────────────────────────────────
    source = _resolve_source(doc)
    if not source:
        _fail(session, doc, EX_FILE_MISSING, error=f"no readable source for {doc.file_name!r}")
        return

    # ── 2. OCR ───────────────────────────────────────────────────────────────
    print(f"[PIPE] {doc.id} OCR starting ({doc.po_type} folder)")
    try:
        ocr = extract_document(source)
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] OCR failed: {e}")
        _fail(session, doc, EX_OCR_FAILED, error=str(e))
        return

    # ── 3. Record what OCR found ─────────────────────────────────────────────
    doc.ocr_document_type = ocr_helpers.get_document_type(ocr)
    doc.vendor_name = ocr_helpers.get_vendor_name(ocr) or doc.vendor_name
    doc.invoice_number = ocr_helpers.get_invoice_number(ocr)
    doc.ro_number = ocr_helpers.get_control_number(ocr)
    if not doc.dealership_name:
        doc.dealership_name = ocr_helpers.get_dealership_name(ocr)
    session.add(doc)
    session.commit()

    if doc.ocr_document_type:
        print(f"[PIPE] {doc.id} OCR type={doc.ocr_document_type!r}, folder={doc.po_type} "
              f"(folder wins)")

    # ── 4. Idempotency ───────────────────────────────────────────────────────
    # Checked after OCR because the invoice number is only known now. A repeat
    # upload is parked rather than posted twice to Tekion.
    duplicate = job_queue.find_duplicate(session, doc)
    if duplicate is not None:
        _fail(
            session,
            doc,
            EX_DUPLICATE,
            error=f"already processed as document {duplicate.id} "
                  f"(invoice {duplicate.invoice_number!r})",
        )
        return

    # ── 5. Dispatch on the folder ────────────────────────────────────────────
    if doc.po_type == FOLDER_OEM:
        _run_journal_entry(doc, ocr, session)
    else:
        _run_purchase_order(doc, ocr, session)


# ── OEM -> Journal entry ──────────────────────────────────────────────────────


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


# ── SUBLET / MISCELLANEOUS / STOCK -> Purchase order ─────────────────────────


def _run_purchase_order(doc: Document, ocr: dict[str, Any], session: Session) -> None:
    """Build the typed PO request from OCR and hand it to the existing flow."""
    from fastapi import HTTPException

    from api.models.schemas import (
        CreateMiscPoRequest,
        CreateStockPoRequest,
        CreateSubletPoRequest,
        MiscLineItem,
        StockPartInput,
        SubletLineItem,
    )
    from api.routes.tekion import _create_misc_po, _create_stock_po, _create_sublet_po

    total = ocr_helpers.get_total_amount(ocr)
    sales_tax = ocr_helpers.get_sales_tax(ocr)
    line_items = ocr_helpers.get_raw_line_items(ocr)

    if not doc.vendor_name or not total:
        _fail(session, doc, EX_MISSING_FIELD, error="missing vendor name or total amount")
        return

    common = {
        "dealership_name": doc.dealership_name,
        "vendor_name": doc.vendor_name,
        "invoice_number": doc.invoice_number,
        "invoice_amount": total,
        "sales_tax": sales_tax,
    }

    try:
        if doc.po_type == FOLDER_SUBLET:
            if not doc.ro_number:
                _fail(session, doc, EX_MISSING_FIELD, error="sublet with no RO number")
                return
            items = [
                SubletLineItem(
                    ro_number=doc.ro_number,
                    # Blank means "first job on the RO", matching the existing flow.
                    job_number="",
                    description=item["description"] or "Sublet repair",
                    labor_amount=0.0,
                    parts_amount=item["totalPrice"] or item["unitPrice"],
                )
                for item in line_items
            ] or [
                SubletLineItem(
                    ro_number=doc.ro_number,
                    job_number="",
                    description="Sublet repair",
                    labor_amount=0.0,
                    parts_amount=total,
                )
            ]
            req = CreateSubletPoRequest(**common, line_items=items)
            with tekion_scope():
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
            req = CreateMiscPoRequest(
                **common,
                line_items=[
                    MiscLineItem(
                        part_name=item["description"] or "Misc purchase",
                        qty=item["qty"],
                        unit_price=item["unitPrice"],
                    )
                    for item in line_items
                ],
            )
            with tekion_scope():
                response = _create_misc_po(req, session)

    except HTTPException as e:
        # The PO flow raises 422 with candidates when a vendor is not mapped.
        detail = e.detail if isinstance(e.detail, dict) else {}
        reason = str(detail.get("reason", "") or e.detail)
        print(f"[PIPE] {doc.id} PO rejected: {reason}")
        _fail(
            session,
            doc,
            EX_VENDOR_NOT_FOUND if "mapping" in reason else EX_TEKION_ERROR,
            error=reason,
        )
        return
    except Exception as e:  # noqa: BLE001
        print(f"[PIPE] {doc.id} PO creation failed: {e}")
        _fail(session, doc, EX_TEKION_ERROR, error=str(e))
        return

    if not response.success:
        _fail(session, doc, EX_TEKION_ERROR, error=response.error or "PO creation failed")
        return

    doc.po_number = response.po_number or ""
    doc.vendor_name = response.vendor_name or doc.vendor_name
    job_queue.complete(session, doc)
    print(f"[PIPE] {doc.id} -> PROCESSED (PO {doc.po_number})")
