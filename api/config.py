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
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

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
