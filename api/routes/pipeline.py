"""Unified upload pipeline routes.

POST /api/pipeline/process        — upload an invoice into a folder, queue it
GET  /api/pipeline/jobs/{doc_id}  — poll the status of one document
GET  /api/pipeline/queue          — queue depth by status
GET  /api/pipeline/folders        — the folders the frontend can upload into

The folder decides which Tekion flow runs (see api/services/pipeline_service.py):
SUBLET / MISCELLANEOUS / STOCK create a purchase order, OEM creates a journal
entry saved as a draft.

Upload only enqueues: it writes the file, creates the `documents` row as QUEUED,
and returns. Background workers (api/services/worker.py) claim and run it. That
way ten simultaneous uploads return instantly and drain in an orderly way rather
than opening ten Tekion sessions at once.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import Document
from api.models.schemas import (
    MessageResponse,
    PipelineAcceptedResponse,
    PipelineStatusResponse,
)
from api.services import job_queue, s3_service
from api.services.pipeline_service import VALID_FOLDERS, normalize_folder

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


@router.get("/folders")
def list_folders() -> dict[str, list[str]]:
    """The valid upload folders, for the frontend to render."""
    return {"folders": sorted(VALID_FOLDERS)}


@router.get("/queue")
def queue_stats(session: Annotated[Session, Depends(get_session)]) -> dict[str, object]:
    """Queue depth by status — how much work is waiting or in flight."""
    counts = job_queue.queue_depth(session)
    return {
        "counts": counts,
        "queued": counts.get(job_queue.STATUS_QUEUED, 0),
        "processing": counts.get(job_queue.STATUS_PROCESSING, 0),
    }


@router.post("/process", response_model=PipelineAcceptedResponse, status_code=202)
async def process_upload(
    session: Annotated[Session, Depends(get_session)],
    file: UploadFile = File(...),
    folder: str = Form(..., description="SUBLET | MISCELLANEOUS | STOCK | OEM"),
    dealership_name: str = Form("", description="Dealership the invoice belongs to"),
) -> PipelineAcceptedResponse:
    """Upload an invoice into a folder and queue it for processing."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    try:
        po_type = normalize_folder(folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Hash the bytes so a straight re-upload of the same file is detectable.
    file_hash = hashlib.sha256(payload).hexdigest()

    # Keep the file on disk for a worker to pick up. Not deleted here — the
    # worker owns it, and a retry needs it to still be there.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    tmp.write(payload)
    tmp.close()

    # Archive to S3 when configured. Best-effort: a storage failure must not
    # stop the document from being processed, and it doubles as the fallback
    # source if the temp file is lost to a restart.
    s3_key = ""
    if s3_service.is_configured():
        try:
            s3_key = s3_service.build_s3_key(file.filename or f"upload{ext}", dealership_name)
            s3_service.upload_file(tmp.name, s3_key)
        except Exception as e:  # noqa: BLE001
            print(f"[PIPE] S3 archive failed ({e}); continuing without it")
            s3_key = ""

    doc = Document(
        file_name=file.filename or "",
        s3_key=s3_key,
        source_path=tmp.name,
        file_hash=file_hash,
        dealership_name=dealership_name,
        po_type=po_type,
        status=job_queue.STATUS_QUEUED,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    print(f"[PIPE] queued {doc.id} ({po_type}, {file.filename})")

    return PipelineAcceptedResponse(
        document_id=doc.id,
        status=doc.status,
        folder=po_type,
        file_name=doc.file_name,
    )


@router.post("/jobs/{document_id}/confirm-duplicate", response_model=PipelineStatusResponse)
def confirm_duplicate(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Reprocess a document that was held as a duplicate.

    The invoice keeps ONE identity: the new upload is folded into the original
    document, which is re-queued with the duplicate check waived for that run,
    and the extra row is dropped. The response is the ORIGINAL document — poll
    that id from here, not the one that was posted to.

    This will create a second record in Tekion. That is the point of confirming,
    but it is worth saying plainly.
    """
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_DUPLICATE:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}, not awaiting a duplicate decision",
        )

    original = job_queue.confirm_duplicate(session, doc)
    return _to_status(original)


@router.post("/jobs/{document_id}/discard", response_model=MessageResponse)
def discard_duplicate(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> MessageResponse:
    """Drop a held duplicate. The original document is untouched."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_DUPLICATE:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}, not awaiting a duplicate decision",
        )

    if doc.source_path:
        try:
            Path(doc.source_path).unlink(missing_ok=True)
        except OSError:
            pass
    session.delete(doc)
    session.commit()
    return MessageResponse(message="Duplicate discarded")


@router.get("/jobs/{document_id}", response_model=PipelineStatusResponse)
def get_job(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Poll one document's progress."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # A split batch does no work itself — the caller polls its children instead.
    children: list[UUID] = []
    if doc.status == job_queue.STATUS_SPLIT:
        children = list(
            session.exec(
                select(Document.id)
                .where(Document.split_from == doc.id)
                .order_by(Document.page_range)
            ).all()
        )

    return _to_status(doc, children)


def _to_status(
    doc: Document, children: list[UUID] | None = None
) -> PipelineStatusResponse:
    return PipelineStatusResponse(
        document_id=doc.id,
        status=doc.status,
        folder=doc.po_type,
        file_name=doc.file_name,
        s3_key=doc.s3_key,
        dealership_name=doc.dealership_name,
        vendor_name=doc.vendor_name,
        invoice_number=doc.invoice_number,
        ro_number=doc.ro_number,
        po_number=doc.po_number,
        transaction_id=doc.transaction_id,
        transaction_number=doc.transaction_number,
        journal_id=doc.journal_id,
        ocr_document_type=doc.ocr_document_type,
        duplicate_of=doc.duplicate_of,
        split_from=doc.split_from,
        page_range=doc.page_range,
        children=children or [],
        exception_type=doc.exception_type,
        severity=doc.severity,
        attempts=doc.attempts,
        last_error=doc.last_error,
        created_at=doc.created_at,
        processed_at=doc.processed_at,
    )
