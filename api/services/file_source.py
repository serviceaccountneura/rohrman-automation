"""Resolve an invoice reference to a readable local file.

Two callers hand us a file in two different ways:

  * /api/pipeline/process uploads the bytes, so it already has a local temp path.
  * The frontend's presigned-upload flow PUTs straight to S3 and only ever holds
    an S3 key — so `invoiceFilePath` arrives as "invoices/schaumburg-honda/..."
    rather than a path on this machine.

`resolve()` accepts either and always returns a local path, downloading from S3
when needed. Anything it downloads is reported as temporary so the caller can
delete it; a path that was already local is left alone.
"""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from api.services import s3_service


def looks_like_s3_key(reference: str) -> bool:
    """Whether a reference is an S3 key rather than a path on this machine.

    A local file that exists wins outright. Otherwise anything shaped like the
    keys generate_upload_url() produces ("invoices/<dealer>/<date>/<id>.pdf") is
    treated as S3.
    """
    if not reference:
        return False
    if Path(reference).exists():
        return False
    if reference.startswith(("s3://", "invoices/")):
        return True
    # A bare relative path with no drive letter and forward slashes is far more
    # likely an S3 key than a local file we simply cannot see.
    return "/" in reference and not Path(reference).is_absolute()


@contextmanager
def resolve(reference: str | None) -> Iterator[str | None]:
    """Yield a local path for `reference`, cleaning up anything downloaded.

    Yields None when there is nothing to resolve, so callers can use this
    unconditionally:

        with file_source.resolve(req.invoice_file_path) as path:
            if path:
                client.upload_document(path)
    """
    if not reference:
        yield None
        return

    if not looks_like_s3_key(reference):
        # Already on disk (or a path we should not second-guess).
        yield reference
        return

    key = reference[5:].split("/", 1)[-1] if reference.startswith("s3://") else reference
    if not s3_service.is_configured():
        raise ValueError(
            f"invoice_file_path {reference!r} looks like an S3 key but AWS credentials "
            "are not configured, so it cannot be downloaded"
        )

    suffix = Path(key).suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    try:
        s3_service.download_file(key, tmp.name)
        print(f"[FILE] downloaded {key} -> {tmp.name}")
        yield tmp.name
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
