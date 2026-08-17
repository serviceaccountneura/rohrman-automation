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
    s3_bucket: str = "rohrman-invoices"
    s3_presign_expiry: int = 300  # seconds (5 min)

    # ── Frontend ──────────────────────────────────────────────────────────────
    frontend_url: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
