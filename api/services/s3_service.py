"""S3 storage for invoice files.

Two jobs:

  * archive every uploaded invoice, so a document that outlives its temp file
    can still be processed. On EC2 that is not a corner case -- every deploy
    restarts the container, and anything queued at that moment loses its local
    copy. `_resolve_source` in the pipeline restores from here.

  * generate presigned upload URLs, so a browser can PUT a file straight to S3
    without proxying through the API.

CREDENTIALS
    Explicit keys from the environment when set; otherwise boto3's own chain,
    which on EC2 means the instance role. Prefer the role in production.

    The BUCKET, not the keys, decides whether archiving is on -- see
    `is_configured`.
"""
from __future__ import annotations

import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig

from api.config import settings

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# boto3's upload_file does not infer a content type, so without this every
# archived file lands as binary/octet-stream and the browser downloads it
# instead of showing it in the viewer.
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}


def _content_type_for(key: str) -> str:
    ext = Path(key).suffix.lower()
    return _CONTENT_TYPES.get(ext) or mimetypes.guess_type(key)[0] or "application/octet-stream"


def _get_s3_client():
    """An S3 client, however this environment supplies credentials.

    Explicit keys win when set, which keeps local development working from a
    .env. When they are absent boto3 falls back to its own chain -- on EC2 that
    is the instance role, which is the better answer in production: nothing
    long-lived sits on the box and the credentials rotate themselves.

    The arguments are built rather than always passed: handing botocore an
    explicit None is not the same as omitting the key, and suppresses the
    fallback chain entirely.
    """
    kwargs: dict = {
        "region_name": settings.aws_region,
        "config": BotoConfig(signature_version="s3v4"),
    }
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs)


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
    """Whether archiving to S3 is switched on.

    Keyed on the BUCKET, not on access keys. On EC2 there are no keys -- the
    instance role supplies them -- so the old check reported "not configured"
    on exactly the deployment where archiving matters most, and silently
    disabled it. A missing bucket name is the real signal that nobody set this
    up.

    Credentials are deliberately not probed: that would mean a network call on
    a path that runs per upload, and a credential problem should surface as a
    logged failure on the archive attempt rather than as a silent skip.

    Archiving stays best-effort either way -- processing must not fail because
    a file could not be stored.
    """
    return bool(settings.s3_bucket)


def upload_file(local_path: str | Path, s3_key: str) -> None:
    """Upload a local file to S3 under `s3_key`, tagged with its real type."""
    _get_s3_client().upload_file(
        str(local_path),
        settings.s3_bucket,
        s3_key,
        ExtraArgs={"ContentType": _content_type_for(s3_key)},
    )


def download_file(s3_key: str, local_path: str | Path) -> None:
    """Fetch an archived file back to disk.

    Used by the queue workers when the temp file is gone (a restart between
    upload and processing) but the row is still waiting.
    """
    _get_s3_client().download_file(settings.s3_bucket, s3_key, str(local_path))


def generate_download_url(s3_key: str) -> str:
    """A temporary GET URL for a file, served for viewing rather than download.

    The response overrides force the right type and `inline` disposition even
    for objects that were stored as octet-stream, so the detail-page viewer
    renders the invoice instead of the browser saving it.
    """
    client = _get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": s3_key,
            "ResponseContentType": _content_type_for(s3_key),
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=settings.s3_presign_expiry,
    )
