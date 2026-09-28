"""Password hashing (argon2) and JWT helpers (one access token per login)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from passlib.context import CryptContext

from api.config import settings

# argon2 is the OWASP-recommended default; bcrypt kept as a fallback for any
# legacy hashes that may exist.
_pwd = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


# ── Password hashing ─────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────


def _expire_minutes() -> timedelta:
    return timedelta(minutes=settings.access_token_expire_minutes)


def create_access_token(
    user_id: UUID,
    email: str,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    """Return (token, jti, expires_at) for an access token."""
    now = datetime.now(timezone.utc)
    exp = now + _expire_minutes()
    jti = secrets.token_urlsafe(16)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "type": "access",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, exp


def decode_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raises jwt.PyJWTError on any failure."""
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
