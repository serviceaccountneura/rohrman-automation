"""OCR routes — PDF upload + background job polling.

POST /api/ocr/extract   — upload PDF/image, returns job_id
GET  /api/ocr/jobs/{id} — poll for extraction result
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile

from api.jobs.manager import job_manager
from api.models.schemas import OcrJobResponse, OcrJobResult, OcrJobStatus
from api.services.ocr_service import extract_document

router = APIRouter(prefix="/api/ocr", tags=["ocr"])

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def _run_extraction(job_id: str, file_path: str) -> None:
    """Background task: run OCR extraction and update job status."""
    try:
        job_manager.update_job(job_id, OcrJobStatus.PROCESSING)
        result = extract_document(file_path)
        job_manager.update_job(job_id, OcrJobStatus.DONE, result=result)
    except Exception as e:
        job_manager.update_job(job_id, OcrJobStatus.ERROR, error=str(e))
    finally:
        # Clean up temp file
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/extract", response_model=OcrJobResponse)
async def upload_and_extract(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> OcrJobResponse:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    content = await file.read()
    tmp.write(content)
    tmp.close()

    job_id = job_manager.create_job()
    background_tasks.add_task(_run_extraction, job_id, tmp.name)

    return OcrJobResponse(job_id=job_id, status=OcrJobStatus.QUEUED)


@router.get("/jobs/{job_id}", response_model=OcrJobResult)
def get_job(job_id: str) -> OcrJobResult:
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return OcrJobResult(
        job_id=job_id,
        status=job["status"],
        result=job.get("result"),
        error=job.get("error"),
    )
