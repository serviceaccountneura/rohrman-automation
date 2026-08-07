"""Invoice routes — presigned S3 upload URLs.

POST /api/invoices/upload-url  — get a presigned URL to upload an invoice file
GET  /api/invoices/{s3_key}    — get a temporary download URL for a file
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.models.schemas import UploadUrlRequest, UploadUrlResponse
from api.services.s3_service import generate_download_url, generate_upload_url

router = APIRouter(prefix="/api/invoices", tags=["invoices"])


@router.post("/upload-url", response_model=UploadUrlResponse)
def create_upload_url(req: UploadUrlRequest) -> UploadUrlResponse:
    try:
        result = generate_upload_url(
            file_name=req.file_name,
            dealership_name=req.dealership_name,
        )
        return UploadUrlResponse(
            upload_url=result["upload_url"],
            s3_key=result["s3_key"],
            file_name=result["file_name"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {e}")


@router.get("/{s3_key:path}")
def get_download_url(s3_key: str) -> dict[str, str]:
    try:
        url = generate_download_url(s3_key)
        return {"download_url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate download URL: {e}")
