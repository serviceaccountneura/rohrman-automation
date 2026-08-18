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
from sqlmodel import Session, select

from api.models.db import Document

STATUS_QUEUED = "QUEUED"
STATUS_PROCESSING = "PROCESSING"
STATUS_PROCESSED = "PROCESSED"
STATUS_EXCEPTION = "EXCEPTION"

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
