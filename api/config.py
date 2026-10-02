"""Application settings loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = (
        "postgresql+psycopg://rohrman:rohrman@localhost:5432/rohrman"
    )

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production-please-use-a-long-random-string"
    jwt_algorithm: str = "HS256"
    # One token per login, no refresh: when it expires the user signs in again.
    # 7 days. Renewing short-lived tokens with single-use refresh tokens raced
    # whenever two requests renewed at once, and a lost race ended the session.
    access_token_expire_minutes: int = 10080

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-2"
    # Empty by default, which switches archiving OFF. Setting the bucket is
    # what turns it on -- so a developer with no AWS access is not retrying a
    # failing upload on every document, and a deployment that forgot to set it
    # is obvious rather than silently degraded.
    s3_bucket: str = ""
    s3_presign_expiry: int = 300  # seconds (5 min)
    # Point S3 somewhere other than AWS -- MinIO on the staging box, say.
    # Empty means real S3, which is what production uses, so nothing changes
    # there. Any S3-compatible server also needs path-style addressing:
    # bucket.host virtual-hosting only resolves for AWS, so another endpoint
    # has to be addressed as host/bucket instead.
    s3_endpoint_url: str = ""
    s3_force_path_style: bool = False
    # The address a BROWSER can fetch a presigned URL from, when that differs
    # from the address this process talks to. With MinIO on the same box the
    # api reaches it at http://minio:9000, which resolves for nobody outside
    # Docker -- so an invoice preview signed for that host never loads.
    #
    # Signing is pure local computation, no request, so the two can differ:
    # uploads and downloads keep using s3_endpoint_url, and only the signature
    # is computed against this one. Empty means "same as s3_endpoint_url",
    # which is the case on real S3 and therefore in production.
    s3_public_endpoint_url: str = ""

    # Clean up each page (crop, enlarge, flatten the background, boost local
    # contrast) before Gemini reads it -- see api/services/page_enhance.py.
    # False sends the page as scanned, as before.
    ocr_enhance: bool = True

    # TESTING BRANCH (staging-no-preinvoice): Misc, Sublet and Vendor Stock
    # read, check and show their GL lines but create NOTHING in Tekion -- no
    # purchase order, no pre-invoice, no uploaded invoice. OEM and Vehicle
    # still save their drafts. Set TEKION_PO_WRITES=true (or change this
    # default) to reconnect; every guarded call is marked "TEKION_PO_WRITES".
    tekion_po_writes: bool = False

    # ── Frontend ──────────────────────────────────────────────────────────────
    frontend_url: str = "http://localhost:3000"

    # ── Outbound email (invites) ──────────────────────────────────────────────
    # Defaults target Gmail, which is free: turn on 2-step verification on the
    # sending account and create an App Password -- a normal Google password is
    # rejected for SMTP. Port 587 is STARTTLS; use 465 for implicit SSL.
    #
    # Leave smtp_user blank to disable sending. Invites are still created and
    # the link is still returned, so the flow works without mail configured --
    # an admin just has to pass the link on themselves.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "Rohrman Invoice Automation"
    # Defaults to smtp_user when blank; Gmail rejects a From it does not own.
    smtp_from_email: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
