"""S3 presigned URL service.

Generates temporary upload URLs so the frontend can PUT invoice files
directly to S3 without proxying through the backend.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig

from api.config import settings

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def _get_s3_client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=BotoConfig(signature_version="s3v4"),
    )


def generate_upload_url(
    file_name: str,
    dealership_name: str | None = None,
) -> dict[str, str]:
    """Generate a presigned S3 PUT URL for an invoice file.

    Args:
        file_name: original file name, e.g. "invoice_001.pdf"
        dealership_name: optional dealership folder prefix

    Returns:
        {"upload_url": "...", "s3_key": "...", "file_name": "..."}
    """
    ext = Path(file_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    # Build a unique S3 key: invoices/{dealership}/{date}/{uuid}.{ext}
    date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    folder = dealership_name.lower().replace(" ", "-") if dealership_name else "unsorted"
    unique_id = uuid.uuid4().hex[:12]
    s3_key = f"invoices/{folder}/{date_str}/{unique_id}{ext}"

    client = _get_s3_client()
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": s3_key,
            "ContentType": "application/octet-stream",
        },
        ExpiresIn=settings.s3_presign_expiry,
    )

    return {
        "upload_url": upload_url,
        "s3_key": s3_key,
        "file_name": file_name,
    }


def build_s3_key(file_name: str, dealership_name: str | None = None) -> str:
    """The canonical key for an invoice file: invoices/{dealership}/{date}/{uuid}.{ext}."""
    ext = Path(file_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    folder = dealership_name.lower().replace(" ", "-") if dealership_name else "unsorted"
    return f"invoices/{folder}/{date_str}/{uuid.uuid4().hex[:12]}{ext}"


def is_configured() -> bool:
    """Whether S3 credentials are present.

    The pipeline treats archiving as best-effort so local runs work without AWS
    configured — processing must not fail just because the file could not be
    stored.
    """
    return bool(settings.aws_access_key_id and settings.aws_secret_access_key)


def upload_file(local_path: str | Path, s3_key: str) -> None:
    """Upload a local file to S3 under `s3_key`."""
    _get_s3_client().upload_file(str(local_path), settings.s3_bucket, s3_key)


def download_file(s3_key: str, local_path: str | Path) -> None:
    """Fetch an archived file back to disk.

    Used by the queue workers when the temp file is gone (a restart between
    upload and processing) but the row is still waiting.
    """
    _get_s3_client().download_file(settings.s3_bucket, s3_key, str(local_path))


def generate_download_url(s3_key: str) -> str:
    """Generate a temporary GET URL for downloading a file from S3."""
    client = _get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key},
        ExpiresIn=settings.s3_presign_expiry,
    )
