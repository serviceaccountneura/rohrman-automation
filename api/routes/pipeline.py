"""Unified upload pipeline routes.

POST /api/pipeline/process        — upload an invoice into a folder, queue it
GET  /api/pipeline/jobs/{doc_id}  — poll the status of one document
GET  /api/pipeline/queue          — queue depth by status
GET  /api/pipeline/folders        — the folders the frontend can upload into

The folder decides which Tekion flow runs (see api/services/pipeline_service.py):
SUBLET / MISCELLANEOUS / STOCK create a purchase order; OEM and
VEHICLE_MANUFACTURING create a journal
entry saved as a draft.

Upload only enqueues: it writes the file, creates the `documents` row as QUEUED,
and returns. Background workers (api/services/worker.py) claim and run it. That
way ten simultaneous uploads return instantly and drain in an orderly way rather
than opening ten Tekion sessions at once.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import Document, User
from api.models.schemas import (
    MessageResponse,
    PipelineAcceptedResponse,
    PipelineStatusResponse,
    PoDecisionRequest,
    ReviewEdit,
    RerunRequest,
)
from api.services import job_queue, misc_review, po_reuse, pipeline_service, s3_service
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
    current_user: CurrentUserDep,
    file: UploadFile = File(...),
    folder: str = Form(..., description="SUBLET | MISCELLANEOUS | STOCK | OEM | VEHICLE_MANUFACTURING"),
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

    # The same file already failed: run it again on that document rather than
    # starting a second one. A rescan of it -- different bytes, same invoice --
    # is caught after OCR instead; see job_queue.absorb_into_failed.
    failed = job_queue.find_failed_same_file(session, file_hash)
    if failed is not None:
        doc = job_queue.reuse_failed_for_upload(
            session,
            failed,
            file_name=file.filename or "",
            s3_key=s3_key,
            source_path=tmp.name,
            file_hash=file_hash,
            dealership_name=dealership_name,
            po_type=po_type,
            uploaded_by_id=current_user.id,
        )
    else:
        doc = Document(
            file_name=file.filename or "",
            s3_key=s3_key,
            source_path=tmp.name,
            file_hash=file_hash,
            dealership_name=dealership_name,
            po_type=po_type,
            status=job_queue.STATUS_QUEUED,
            uploaded_by_id=current_user.id,
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
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_DUPLICATE:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}, not awaiting a duplicate decision",
        )

    original = job_queue.confirm_duplicate(session, doc)
    return _to_status(original, session=session)


@router.post("/jobs/{document_id}/discard", response_model=MessageResponse)
def discard_duplicate(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> MessageResponse:
    """Drop a held duplicate. The original document is untouched."""
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
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


def _review_payload(doc: Document) -> dict | None:
    draft = misc_review.load(doc.review_draft)
    if not draft:
        return None
    return {**draft, "balance": misc_review.balance(draft)}


def _held_for_review(document_id: UUID, session: Session) -> Document:
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_AWAITING_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}, not waiting for review",
        )
    return doc


def _chart(dealer_id: str, session: Session) -> dict[str, str]:
    """{account number: account name} for a dealership, from the cached chart."""
    from api.services.gl_service_misc import get_cached_gl_accounts

    if not dealer_id:
        return {}
    return {
        str(a.account_number): str(a.account_name)
        for a in get_cached_gl_accounts(dealer_id, session)
        if a.account_number
    }


@router.get("/jobs/{document_id}/gl-accounts")
def review_gl_accounts(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> list[dict[str, str]]:
    """The chart of accounts for the dealership this document belongs to.

    For the editor: an account number is typed, and its name should appear as
    it is typed, without a round trip per keystroke.
    """
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    dealer_id = misc_review.load(doc.review_draft).get("dealerId") or ""
    return [
        {"number": number, "name": name}
        for number, name in sorted(_chart(dealer_id, session).items())
    ]


@router.put("/jobs/{document_id}/review", response_model=PipelineStatusResponse)
def save_review(
    document_id: UUID,
    payload: ReviewEdit,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Save a correction to a document held for review. Nothing is posted.

    Saving is not validation. A half-finished edit -- a line added but not yet
    given an amount -- is allowed to be saved and come back to; the checks run
    when the document is posted.
    """
    doc = _held_for_review(document_id, session)
    draft = misc_review.load(doc.review_draft)
    updated = misc_review.apply_edit(
        draft,
        fields=payload.fields,
        lines=payload.lines,
        names=_chart(draft.get("dealerId") or "", session),
    )
    doc.review_draft = misc_review.dump(updated)

    # The row in the documents table reads these, and should say what the
    # person corrected rather than what OCR first read.
    fields = updated.get("fields") or {}
    doc.vendor_name = fields.get("vendorName") or doc.vendor_name
    doc.invoice_number = fields.get("invoiceNumber") or doc.invoice_number
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return _to_status(doc, session=session)


@router.post("/jobs/{document_id}/post", response_model=PipelineStatusResponse)
def post_reviewed(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Release a reviewed document to Tekion.

    Everything that can be checked without Tekion is checked HERE, while the
    person is still looking at the screen: every account exists at this
    dealership, the lines add up, the vendor is mapped. A refusal comes back as
    a 422 naming each problem, and the document stays in review.

    What cannot be checked in advance -- Tekion itself refusing the purchase
    order -- still ends in EXCEPTION, as any other run does.
    """
    from api.services.vendor_service import _find_mapping

    doc = _held_for_review(document_id, session)
    draft = misc_review.load(doc.review_draft)
    dealer_id = draft.get("dealerId") or ""

    problems = misc_review.problems(draft, set(_chart(dealer_id, session)))

    vendor = str((draft.get("fields") or {}).get("vendorName") or "")
    if vendor and dealer_id and _find_mapping(dealer_id, vendor, session) is None:
        problems.append(
            f"The vendor '{vendor}' is not mapped to a Tekion vendor at this dealership."
        )

    if problems:
        # Kept on the draft too, so the refusal is still there after a reload.
        draft["error"] = " ".join(problems)
        doc.review_draft = misc_review.dump(draft)
        session.add(doc)
        session.commit()
        raise HTTPException(status_code=422, detail=problems)

    return _to_status(job_queue.approve_review(session, doc), session=session)


@router.delete("/jobs/{document_id}", response_model=PipelineStatusResponse)
def delete_document(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> PipelineStatusResponse:
    """Take a document out of view. The record is kept.

    Nothing that already happened is undone: a purchase order that was created
    stays created and an invoice that posted stays posted. Tekion is not called.

    What it does stop is future work -- a document still waiting in the queue
    will not be picked up, and it no longer blocks a re-upload as a duplicate,
    which is what makes "delete it and try again" work.

    Deleting an already-deleted document is not an error; it is already in the
    state the caller asked for.
    """
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.deleted_at is None:
        job_queue.soft_delete(session, doc, user_id=current_user.id)
    return _to_status(doc, session=session)


@router.post("/jobs/{document_id}/restore", response_model=PipelineStatusResponse)
def restore_document(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Put a deleted document back in view, in the state it was left in.

    It is NOT re-queued. The row comes back saying what it said before, and
    whoever restored it decides what to do next -- re-running as a side effect
    of un-hiding a row would post to Tekion without anyone asking.
    """
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.deleted_at is not None:
        job_queue.restore(session, doc)
    return _to_status(doc, session=session)


@router.post("/jobs/{document_id}/po-decision", response_model=PipelineStatusResponse)
def decide_purchase_order(
    document_id: UUID,
    payload: PoDecisionRequest,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Say whether to invoice the existing purchase order or raise a new one.

    The document is held in PO_DECISION because it names a PO that already
    exists in Tekion, and posting against an order somebody else raised is not
    a call the pipeline makes on its own.

    The choice applies to THIS run only. A later upload of the same invoice is
    asked again rather than inheriting a decision made about different
    paperwork.
    """
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_PO_DECISION:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}, not awaiting a purchase order decision",
        )

    choice = (
        po_reuse.CHOICE_EXISTING
        if payload.choice == "existing"
        else po_reuse.CHOICE_NEW
    )
    return _to_status(job_queue.resolve_po_decision(session, doc, choice), session=session)


@router.post("/jobs/{document_id}/rerun", response_model=PipelineStatusResponse)
def rerun_document(
    document_id: UUID,
    payload: RerunRequest,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Run a refused document again, with fields a person supplied.

    The invoice is not read again. OCR from the first attempt is cached, the
    corrections are overlaid on it, and the document goes back on the queue --
    so a missing stock number is fixed in seconds without another Gemini pass,
    and without needing the uploaded file, which is usually gone by now.

    Only a document in EXCEPTION can be re-run. A PROCESSED one has already
    posted to Tekion, and running it again would create a second record there;
    that path is `confirm-duplicate`, which says what it does.
    """
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != job_queue.STATUS_EXCEPTION:
        raise HTTPException(
            status_code=409,
            detail=f"Document is {doc.status}; only a failed document can be re-run",
        )

    # Merged with anything supplied on an earlier re-run, so a second correction
    # does not discard the first.
    fields = pipeline_service.manual_overrides(doc)
    supplied = payload.model_dump(exclude_defaults=True, by_alias=False)
    for key, value in supplied.items():
        if key == "gl_annotations":
            merged = dict(fields.get("gl_annotations") or {})
            merged.update({k: v for k, v in (value or {}).items() if str(v).strip()})
            if merged:
                fields["gl_annotations"] = merged
        elif str(value).strip():
            fields[key] = value

    if not fields:
        raise HTTPException(
            status_code=400,
            detail="No corrections supplied. Fill in at least one field before re-running.",
        )

    doc.manual_fields = json.dumps(fields)[:4000]
    job_queue.requeue_for_rerun(session, doc)
    print(f"[PIPE] {doc.id} re-run requested with {fields}")
    return _to_status(doc, session=session)


@router.get("/jobs/{document_id}", response_model=PipelineStatusResponse)
def get_job(
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PipelineStatusResponse:
    """Poll one document's progress."""
    doc = session.get(Document, job_queue.resolve_id(session, document_id))
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

    return _to_status(doc, children, session=session)


def _as_json_object(raw: str) -> dict:
    """A stored JSON column as a dict, or {} for anything unreadable.

    These columns are written by this application and never by a user, so bad
    JSON means a bug rather than an attack -- but a detail page should still
    render without it rather than 500 on a row somebody truncated.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_status(
    doc: Document,
    children: list[UUID] | None = None,
    session: Session | None = None,
) -> PipelineStatusResponse:
    uploaded_by = ""
    if session is not None and doc.uploaded_by_id is not None:
        uploader = session.get(User, doc.uploaded_by_id)
        if uploader is not None:
            uploaded_by = uploader.full_name or uploader.email
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
        po_candidate=_as_json_object(doc.po_candidate) or None,
        review_draft=_review_payload(doc),
        manual_fields=_as_json_object(doc.manual_fields),
        vehicle_details=_as_json_object(doc.vehicle_details),
        posting_details=_as_json_object(doc.posting_details),
        needs_fields=[
            str(f) for f in (_as_json_object(doc.vehicle_details).get("needs") or [])
        ],
        split_from=doc.split_from,
        page_range=doc.page_range,
        children=children or [],
        exception_type=doc.exception_type,
        severity=doc.severity,
        attempts=doc.attempts,
        last_error=doc.last_error,
        uploaded_by=uploaded_by,
        created_at=doc.created_at,
        processed_at=doc.processed_at,
        deleted_at=doc.deleted_at,
    )
