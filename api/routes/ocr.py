"""OCR routes — extraction with background job polling.

POST /api/ocr/extract   — upload a file OR name an S3 key, returns job_id
GET  /api/ocr/jobs/{id} — poll for the extraction result

This is the standalone OCR step used by the presigned-upload flow: the frontend
PUTs the file to S3, calls this with the returned `s3Key`, polls, shows the
extracted values, then calls POST /api/tekion/po itself.

(The one-call alternative is POST /api/pipeline/process, which does the upload,
OCR and Tekion work in a single request and queues it. Both are supported; this
one leaves the caller in control of each step.)

DB: a `documents` row is created per extraction so this flow is tracked too, in
the same table the pipeline uses. It is created PENDING rather than QUEUED so
the pipeline workers do not pick it up — the caller drives what happens next.
Pass the returned `document_id` to POST /api/tekion/po to have the PO result
recorded against the same row.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from api.db import engine, get_session
from api.jobs.manager import job_manager
from api.models.db import Document
from api.models.schemas import OcrJobResponse, OcrJobResult, OcrJobStatus
from api.services import s3_service
from api.services.ocr_service import extract_document

router = APIRouter(prefix="/api/ocr", tags=["ocr"])

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def _run_extraction(job_id: str, file_path: str, document_id: UUID | None) -> None:
    """Background task: run OCR, update the job, and record fields on the document."""
    try:
        job_manager.update_job(job_id, OcrJobStatus.PROCESSING)
        result = extract_document(file_path)
        job_manager.update_job(job_id, OcrJobStatus.DONE, result=result)

        if document_id is not None:
            fields = result.get("_fields") or {}
            with Session(engine) as session:
                doc = session.get(Document, document_id)
                if doc is not None:
                    doc.ocr_document_type = fields.get("document_type", "")
                    doc.vendor_name = fields.get("vendor_name", "")
                    doc.invoice_number = fields.get("invoice_number", "")
                    doc.ro_number = fields.get("ro_number", "")
                    if not doc.dealership_name:
                        doc.dealership_name = fields.get("dealership_name", "")
                    session.add(doc)
                    session.commit()
    except Exception as e:
        job_manager.update_job(job_id, OcrJobStatus.ERROR, error=str(e))
        if document_id is not None:
            with Session(engine) as session:
                doc = session.get(Document, document_id)
                if doc is not None:
                    doc.status = "EXCEPTION"
                    doc.exception_type = "OCR_FAILED"
                    doc.severity = "HIGH"
                    doc.last_error = str(e)[:1000]
                    session.add(doc)
                    session.commit()
    finally:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/extract", response_model=OcrJobResponse)
async def upload_and_extract(
    background_tasks: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    file: UploadFile | None = File(None, description="The invoice file"),
    s3_key: str = Form("", description="S3 key from /api/invoices/upload-url (instead of file)"),
    dealership_name: str = Form(""),
    po_type: str = Form("", description="SUBLET | MISCELLANEOUS | STOCK | OEM (for tracking)"),
) -> OcrJobResponse:
    """Extract an invoice. Supply either an uploaded `file` or an `s3_key`."""
    if file is None and not s3_key:
        raise HTTPException(status_code=400, detail="Provide either 'file' or 's3_key'")

    file_name = file.filename if file is not None else Path(s3_key).name
    ext = Path(file_name or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        if file is not None:
            tmp.write(await file.read())
            tmp.close()
        else:
            tmp.close()
            if not s3_service.is_configured():
                raise HTTPException(
                    status_code=400,
                    detail="s3_key given but AWS credentials are not configured",
                )
            s3_service.download_file(s3_key, tmp.name)
    except HTTPException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    except Exception as e:
        Path(tmp.name).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not read S3 key {s3_key!r}: {e}") from e

    # Track this extraction in the same table the pipeline uses. PENDING, not
    # QUEUED, so the pipeline workers leave it alone — this flow is caller-driven.
    doc = Document(
        file_name=file_name or "",
        s3_key=s3_key,
        dealership_name=dealership_name,
        po_type=po_type.upper(),
        status="PENDING",
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    job_id = job_manager.create_job()
    background_tasks.add_task(_run_extraction, job_id, tmp.name, doc.id)

    return OcrJobResponse(job_id=job_id, status=OcrJobStatus.QUEUED, document_id=doc.id)


@router.get("/jobs/{job_id}", response_model=OcrJobResult)
def get_job(job_id: str) -> OcrJobResult:
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = job.get("result")
    return OcrJobResult(
        job_id=job_id,
        status=job["status"],
        result=result,
        # The flat, predictable field names — use these rather than digging
        # through result.identifiers[] / result.totals[].
        fields=(result or {}).get("_fields") if result else None,
        error=job.get("error"),
    )
