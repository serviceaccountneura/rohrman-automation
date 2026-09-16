"""DB-backed job queue over the `documents` table.

Why the documents table and not a separate jobs table: the document *is* the
job. Keeping them together means one status field, one row to poll, and the
dashboard shows queue state without any extra wiring.

Claiming uses Postgres' SELECT ... FOR UPDATE SKIP LOCKED, the standard
DB-queue primitive: each worker locks a different row instead of fighting over
the same one, and a crashed worker's lock is released when its transaction dies.

Lifecycle:

    QUEUED ──claim──> PROCESSING ──ok───> PROCESSED
                          │
                          ├──fail, attempts < MAX──> QUEUED (next_attempt_at set)
                          └──fail, attempts >= MAX─> EXCEPTION

A row is invisible to claims while `next_attempt_at` is in the future, which is
how backoff works without a scheduler.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from typing import Any

from sqlmodel import Session, select

from api.models.db import Document

STATUS_QUEUED = "QUEUED"
STATUS_PROCESSING = "PROCESSING"
STATUS_PROCESSED = "PROCESSED"
STATUS_EXCEPTION = "EXCEPTION"
# Held for a human decision, not a failure — nothing was sent to Tekion.
STATUS_DUPLICATE = "DUPLICATE"
# The invoice names a purchase order that already exists in Tekion. Like
# DUPLICATE this is a question, not a failure -- nothing was posted.
STATUS_PO_DECISION = "PO_DECISION"
# Read and decided, not yet posted. A person checks the fields and GL lines and
# releases it. Nothing has been sent to Tekion.
STATUS_AWAITING_REVIEW = "AWAITING_REVIEW"
# A batch scan that was broken into one child document per invoice. Terminal:
# the parent itself is never processed, its children carry the actual work.
STATUS_SPLIT = "SPLIT"

# Attempts per document before it is parked as an EXCEPTION.
MAX_ATTEMPTS = 3

# Exponential-ish backoff between attempts.
_BACKOFF_SECONDS = {1: 30, 2: 120}

# A row PROCESSING for longer than this is assumed abandoned (worker crashed or
# the process was killed mid-job) and is returned to the queue.
STALE_LOCK_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_next(session: Session, worker_id: str) -> Document | None:
    """Atomically claim the oldest runnable job. Returns None when idle.

    SKIP LOCKED means concurrent workers never block each other and never claim
    the same row.
    """
    row = session.exec(
        select(Document)
        .where(
            Document.status == STATUS_QUEUED,
            # Deleted while it sat in the queue. Nothing has been posted yet,
            # and posting it now would be the one thing the person who deleted
            # it was trying to prevent.
            Document.deleted_at.is_(None),  # type: ignore[union-attr]
            (Document.next_attempt_at.is_(None))  # type: ignore[union-attr]
            | (Document.next_attempt_at <= _utcnow()),  # type: ignore[operator]
        )
        .order_by(Document.created_at)  # type: ignore[arg-type]
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()

    if row is None:
        return None

    row.status = STATUS_PROCESSING
    row.attempts += 1
    row.locked_at = _utcnow()
    row.locked_by = worker_id
    session.add(row)
    session.commit()
    session.refresh(row)
    print(f"[QUEUE] {worker_id} claimed {row.id} ({row.po_type}, attempt {row.attempts})")
    return row


def complete(session: Session, doc: Document) -> None:
    """Mark a job finished successfully."""
    doc.status = STATUS_PROCESSED
    doc.exception_type = None
    doc.severity = None
    doc.last_error = ""
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = _utcnow()
    session.add(doc)
    session.commit()


def fail(
    session: Session,
    doc: Document,
    exception_type: str,
    severity: str = "HIGH",
    error: str = "",
    retryable: bool = False,
) -> None:
    """Mark a job failed — requeue it with backoff, or park it as an EXCEPTION.

    `retryable` should be True only for transient problems (network, Tekion 5xx).
    A missing invoice number will not fix itself, so retrying it just burns
    attempts and delays the human seeing it.
    """
    doc.last_error = (error or "")[:1000]
    doc.locked_at = None
    doc.locked_by = ""

    if retryable and doc.attempts < MAX_ATTEMPTS:
        delay = _BACKOFF_SECONDS.get(doc.attempts, 300)
        doc.status = STATUS_QUEUED
        doc.next_attempt_at = _utcnow() + timedelta(seconds=delay)
        session.add(doc)
        session.commit()
        print(f"[QUEUE] {doc.id} retry {doc.attempts}/{MAX_ATTEMPTS} in {delay}s ({exception_type})")
        return

    doc.status = STATUS_EXCEPTION
    doc.exception_type = exception_type
    doc.severity = severity
    doc.next_attempt_at = None
    doc.processed_at = _utcnow()
    session.add(doc)
    session.commit()
    print(f"[QUEUE] {doc.id} -> EXCEPTION ({exception_type})")


def hold_as_duplicate(session: Session, doc: Document, original: Document) -> None:
    """Park a run that repeats an already-processed invoice.

    Deliberately not an exception: nothing went wrong and nothing was posted.
    Someone decides whether to reprocess it (see confirm_duplicate) or discard
    it, and until they do the row simply waits.
    """
    doc.status = STATUS_DUPLICATE
    doc.duplicate_of = original.id
    doc.exception_type = None
    doc.severity = None
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = _utcnow()
    doc.last_error = (
        f"Matches invoice {original.invoice_number or '(unknown)'} "
        f"already processed on {original.created_at:%d %b %Y}"
    )
    session.add(doc)
    session.commit()
    print(f"[QUEUE] {doc.id} -> DUPLICATE of {original.id}")


def hold_for_po_decision(session: Session, doc: Document, found: Any, summary: str) -> None:
    """Park a run whose invoice names a purchase order that already exists.

    Deliberately not an exception, for the same reason a duplicate is not:
    nothing went wrong and nothing was posted. Someone says whether to invoice
    the PO that is already there or raise a new one, and until they do the row
    waits.
    """
    doc.status = STATUS_PO_DECISION
    doc.po_number = found.po_number or doc.po_number
    doc.po_candidate = found.as_json(doc.vendor_name)
    doc.po_choice = ""
    doc.exception_type = None
    doc.severity = None
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = _utcnow()
    doc.last_error = summary[:1000]
    session.add(doc)
    session.commit()
    print(f"[QUEUE] {doc.id} -> PO_DECISION ({found.po_number})")


def hold_for_review(session: Session, doc: Document, draft_json: str) -> None:
    """Park a document with what the flow would post, for a person to check.

    Not an exception: nothing went wrong. The flow has done everything except
    the one step that cannot be taken back.
    """
    doc.status = STATUS_AWAITING_REVIEW
    doc.review_draft = draft_json
    doc.review_approved = False
    doc.exception_type = None
    doc.severity = None
    doc.last_error = ""
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = _utcnow()
    session.add(doc)
    session.commit()
    print(f"[QUEUE] {doc.id} -> AWAITING_REVIEW")


def approve_review(session: Session, doc: Document) -> Document:
    """Release a reviewed document to be posted, exactly as it now stands."""
    doc.review_approved = True
    doc.status = STATUS_QUEUED
    doc.attempts = 0
    doc.exception_type = None
    doc.severity = None
    doc.last_error = ""
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = None
    session.add(doc)
    session.commit()
    session.refresh(doc)
    print(f"[QUEUE] {doc.id} review approved -- queued to post")
    return doc


def resolve_po_decision(session: Session, doc: Document, choice: str) -> Document:
    """Re-queue a held document with the person's choice recorded.

    The choice is consumed by the next run and cleared there, so it applies to
    this attempt only -- a later upload of the same invoice asks again rather
    than silently repeating a decision made about a different day's paperwork.
    """
    doc.po_choice = choice
    doc.status = STATUS_QUEUED
    doc.attempts = 0
    doc.exception_type = None
    doc.severity = None
    doc.last_error = ""
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    doc.processed_at = None
    session.add(doc)
    session.commit()
    session.refresh(doc)
    print(f"[QUEUE] {doc.id} PO decision: {choice} -- re-queued")
    return doc


def confirm_duplicate(session: Session, doc: Document) -> Document:
    """Reprocess a duplicate against the ORIGINAL document, not a second one.

    The point of the confirmation is that the invoice keeps one identity. The
    newly uploaded file and everything OCR read from it are moved onto the
    original row, the original is re-queued with the duplicate check waived for
    one run, and the extra row is dropped.

    Returns the original document — the caller should follow that id from here.
    """
    original = session.get(Document, doc.duplicate_of) if doc.duplicate_of else None
    if original is None:
        # The original was deleted while this sat in review; the duplicate is
        # no longer a duplicate, so let it run on its own.
        doc.status = STATUS_QUEUED
        doc.duplicate_of = None
        doc.duplicate_override = True
        doc.last_error = ""
        doc.processed_at = None
        session.add(doc)
        session.commit()
        return doc

    # Carry the new upload over: the file itself, and what OCR made of it.
    original.file_name = doc.file_name or original.file_name
    original.source_path = doc.source_path
    original.s3_key = doc.s3_key or original.s3_key
    original.file_hash = doc.file_hash or original.file_hash
    original.dealership_name = doc.dealership_name or original.dealership_name
    original.po_type = doc.po_type or original.po_type
    original.vendor_name = doc.vendor_name or original.vendor_name
    original.invoice_number = doc.invoice_number or original.invoice_number
    original.ro_number = doc.ro_number or original.ro_number
    original.ocr_document_type = doc.ocr_document_type or original.ocr_document_type
    # The person who re-uploaded and confirmed now owns this row's result.
    original.uploaded_by_id = doc.uploaded_by_id or original.uploaded_by_id

    # Previous Tekion references belong to the earlier run and would be
    # misleading if this one fails. The UI shows them before confirming.
    original.po_number = ""
    original.transaction_id = ""
    original.transaction_number = ""
    original.journal_id = ""

    original.status = STATUS_QUEUED
    original.duplicate_override = True
    original.duplicate_of = None
    original.attempts = 0
    original.exception_type = None
    original.severity = None
    original.last_error = ""
    original.locked_at = None
    original.locked_by = ""
    original.next_attempt_at = None
    original.processed_at = None

    session.add(original)
    # The duplicate row hands over its file, so do not delete it from disk here.
    doc.source_path = ""
    session.delete(doc)
    session.commit()
    session.refresh(original)
    print(f"[QUEUE] duplicate confirmed -- re-running {original.id}")
    return original


def requeue_for_rerun(session: Session, doc: Document) -> Document:
    """Put a failed document back on the queue after a person corrected it.

    The attempt counter is reset. Retries exist to ride out a flaky Tekion, and
    this is not a retry -- the inputs changed, so the previous failures say
    nothing about whether this run will work, and letting them count would
    exhaust the budget on a document that is now correct.

    The previous error is cleared for the same reason: leaving it visible next
    to a QUEUED row reads as a fresh failure.
    """
    doc.status = STATUS_QUEUED
    doc.exception_type = None
    doc.severity = None
    doc.last_error = ""
    doc.attempts = 0
    doc.next_attempt_at = None
    doc.locked_at = None
    doc.locked_by = ""
    doc.processed_at = None
    session.add(doc)
    session.commit()
    session.refresh(doc)
    print(f"[QUEUE] {doc.id} -> QUEUED (re-run with corrections)")
    return doc


def requeue_stale(session: Session) -> int:
    """Return abandoned PROCESSING rows to the queue.

    A worker that dies mid-job leaves its row PROCESSING forever. This is the
    sweeper that rescues them; the poller calls it periodically.
    """
    cutoff = _utcnow() - timedelta(minutes=STALE_LOCK_MINUTES)
    stale = session.exec(
        select(Document).where(
            Document.status == STATUS_PROCESSING,
            Document.locked_at.is_not(None),  # type: ignore[union-attr]
            Document.locked_at < cutoff,  # type: ignore[operator]
        )
    ).all()

    for row in stale:
        if row.attempts >= MAX_ATTEMPTS:
            row.status = STATUS_EXCEPTION
            row.exception_type = "WORKER_ABANDONED"
            row.severity = "HIGH"
            row.processed_at = _utcnow()
        else:
            row.status = STATUS_QUEUED
            row.next_attempt_at = None
        row.locked_at = None
        row.locked_by = ""
        row.last_error = "worker did not finish; row was reclaimed"
        session.add(row)

    if stale:
        session.commit()
        print(f"[QUEUE] reclaimed {len(stale)} stale job(s)")
    return len(stale)


def soft_delete(session: Session, doc: Document, user_id: Any = None) -> Document:
    """Take a document out of view without destroying it.

    Nothing is undone. A PO that was created stays created and an invoice that
    posted stays posted -- this is a list being tidied, not a reversal, and
    pretending otherwise would be worse than leaving the row visible.

    What it does stop is future work: a document still QUEUED will not be picked
    up, and it no longer blocks a re-upload as a duplicate.
    """
    doc.deleted_at = _utcnow()
    doc.deleted_by_id = user_id
    # Release the queue lock if it holds one, so a crashed worker's sweep does
    # not later resurrect it.
    doc.locked_at = None
    doc.locked_by = ""
    doc.next_attempt_at = None
    session.add(doc)
    session.commit()
    session.refresh(doc)
    print(f"[QUEUE] {doc.id} -> deleted (kept on record)")
    return doc


def restore(session: Session, doc: Document) -> Document:
    """Put a deleted document back in view, in the state it was left in.

    Deliberately does NOT re-queue it. The row comes back saying what it said
    before -- processed, refused, held -- and whoever restored it decides what
    to do next. Re-running on restore would post to Tekion as a side effect of
    un-hiding a row.
    """
    doc.deleted_at = None
    doc.deleted_by_id = None
    session.add(doc)
    session.commit()
    session.refresh(doc)
    print(f"[QUEUE] {doc.id} -> restored ({doc.status})")
    return doc


def find_duplicate(session: Session, doc: Document) -> Document | None:
    """An earlier document that already produced this same work.

    Two checks, cheapest first:
      1. identical file bytes (same SHA-256) — a straight re-upload
      2. same dealership + invoice number + folder — the same invoice rescanned

    Only PROCESSED rows count: a previous failure should not block a retry.
    """
    if doc.file_hash:
        same_file = session.exec(
            select(Document).where(
                Document.file_hash == doc.file_hash,
                Document.id != doc.id,
                Document.status == STATUS_PROCESSED,
                # A deleted record is not something to collide with: deleting it
                # and uploading it again is how a person corrects a bad run, and
                # holding the new one as a duplicate of the discarded one would
                # make that impossible.
                Document.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
        if same_file:
            return same_file

    if doc.invoice_number and doc.dealership_name:
        same_invoice = session.exec(
            select(Document).where(
                Document.invoice_number == doc.invoice_number,
                Document.dealership_name == doc.dealership_name,
                Document.po_type == doc.po_type,
                Document.deleted_at.is_(None),  # type: ignore[union-attr]
                Document.id != doc.id,
                Document.status == STATUS_PROCESSED,
            )
        ).first()
        if same_invoice:
            return same_invoice

    return None


def queue_depth(session: Session) -> dict[str, int]:
    """Counts per status — for the queue-stats endpoint."""
    counts: dict[str, int] = {}
    rows = session.exec(
        text("SELECT status, COUNT(*) FROM documents GROUP BY status")  # type: ignore[arg-type]
    ).all()
    for status, count in rows:
        counts[status] = count
    return counts
