"""Auth routes — signup, login, refresh, me.

POST   /api/auth/signup   — register a new user
POST   /api/auth/login    — authenticate, returns access + refresh tokens
POST   /api/auth/refresh  — exchange a refresh token for a new token pair
POST   /api/auth/logout   — revoke the supplied refresh token
GET    /api/auth/me       — return the current authenticated user
POST   /api/auth/password-reset/request  — email a reset link
GET    /api/auth/password-reset/{token}  — is this link still good?
POST   /api/auth/password-reset/confirm  — set a new password
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from api.config import settings
from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import (
    InviteCode,
    PasswordReset,
    RefreshToken,
    User,
    _utcnow,
    is_expired,
)
from api.models.schemas import (
    InviteValidateResponse,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetValidateResponse,
    RefreshRequest,
    Token,
    UserCreate,
    UserLogin,
    UserRead,
)
from api.services.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _build_token_pair(user: User, session: Session) -> Token:
    access_token, _, _ = create_access_token(user.id, user.email)
    refresh_token, jti, exp, token_hash = create_refresh_token(user.id, user.email)

    session.add(
        RefreshToken(
            user_id=user.id,
            jti=jti,
            token_hash=token_hash,
            expires_at=exp,
        )
    )
    session.commit()

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def signup(req: UserCreate, session: Annotated[Session, Depends(get_session)]) -> User:
    existing = session.exec(select(User).where(User.email == req.email)).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists",
        )

    # Validate invite code if provided
    invite = None
    if req.invite_code:
        invite = session.exec(
            select(InviteCode).where(InviteCode.code == req.invite_code)
        ).first()
        if not invite:
            raise HTTPException(status_code=400, detail="Invalid invite code")
        if invite.used:
            raise HTTPException(status_code=400, detail="Invite code already used")
        if is_expired(invite.expires_at):
            raise HTTPException(status_code=400, detail="Invite code has expired")
        # The invite names who it is for. Without this check a forwarded link
        # would let anyone create an account under any address, which defeats
        # the point of inviting a specific person.
        if invite.email and invite.email.strip().lower() != req.email.strip().lower():
            raise HTTPException(
                status_code=400,
                detail=f"This invitation was sent to {invite.email}. "
                       "Sign up with that address.",
            )

    role = invite.role if invite else "AP_CLERK"
    full_name = req.full_name or (invite.full_name if invite else None)

    user = User(
        email=req.email,
        full_name=full_name,
        hashed_password=hash_password(req.password),
        role=role,
        # Whatever the invite granted. Empty means every dealership, which is
        # what someone signing up without an invite gets.
        dealerships=(invite.dealerships if invite else ""),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # Mark invite as used
    if invite:
        invite.used = True
        invite.used_by = user.id
        invite.used_at = datetime.now(timezone.utc)
        session.add(invite)
        session.commit()

    return user


@router.get("/invite/{code}", response_model=InviteValidateResponse)
def validate_invite(
    code: str,
    session: Annotated[Session, Depends(get_session)],
) -> InviteValidateResponse:
    """Validate an invite code. Public — called by the frontend invite page.

    Returns whether the code is valid, and pre-fills role + name if available.
    """
    invite = session.exec(select(InviteCode).where(InviteCode.code == code)).first()
    if not invite:
        return InviteValidateResponse(valid=False, reason="Invalid invite code")

    if invite.used:
        return InviteValidateResponse(valid=False, reason="Invite code already used")

    if is_expired(invite.expires_at):
        return InviteValidateResponse(valid=False, reason="Invite code has expired")

    return InviteValidateResponse(
        valid=True,
        email=invite.email or None,
        role=invite.role,
        full_name=invite.full_name,
        expires_at=invite.expires_at,
    )


@router.post("/login", response_model=Token)
def login(req: UserLogin, session: Annotated[Session, Depends(get_session)]) -> Token:
    user = session.exec(select(User).where(User.email == req.email)).first()
    if user is None or not verify_password(req.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return _build_token_pair(user, session)


@router.post("/refresh", response_model=Token)
def refresh(
    req: RefreshRequest,
    session: Annotated[Session, Depends(get_session)],
) -> Token:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(req.refresh_token)
    except jwt.PyJWTError as e:
        raise credentials_exc from e

    if payload.get("type") != "refresh":
        raise credentials_exc

    jti = payload.get("jti")
    if not jti:
        raise credentials_exc

    stored = session.exec(
        select(RefreshToken).where(RefreshToken.jti == jti)
    ).first()
    if stored is None or stored.revoked:
        raise credentials_exc

    # Defense in depth: verify the presented token matches the stored hash.
    if stored.token_hash != hash_refresh_token(req.refresh_token):
        raise credentials_exc

    if stored.expires_at < datetime.now(stored.expires_at.tzinfo):
        raise credentials_exc

    user = session.exec(select(User).where(User.id == stored.user_id)).first()
    if user is None or not user.is_active:
        raise credentials_exc

    # Rotate: revoke the old refresh token, issue a fresh pair.
    stored.revoked = True
    session.add(stored)
    session.commit()

    return _build_token_pair(user, session)


@router.post("/logout", response_model=MessageResponse)
def logout(
    req: RefreshRequest,
    session: Annotated[Session, Depends(get_session)],
) -> MessageResponse:
    try:
        payload = decode_token(req.refresh_token)
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        ) from e

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    stored = session.exec(
        select(RefreshToken).where(RefreshToken.jti == jti)
    ).first()
    if stored is None:
        # Idempotent: already gone / never existed.
        return MessageResponse(message="Logged out")
    stored.revoked = True
    session.add(stored)
    session.commit()
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=UserRead)
def me(current_user: CurrentUserDep) -> User:
    return current_user


# ── Password reset ───────────────────────────────────────────────────────────
#
# One hour, not the invite's 24. An invite is expected to sit in an inbox until
# someone gets round to it; a reset is requested by a person who is at their
# keyboard right now, so a long window only widens the time a stolen link works.
_RESET_TTL = timedelta(hours=1)


def _reset_url(token: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"


@router.post("/password-reset/request", response_model=MessageResponse)
def request_password_reset(
    payload: PasswordResetRequest,
    session: Annotated[Session, Depends(get_session)],
) -> MessageResponse:
    """Email a reset link, if that address has an account.

    ALWAYS returns the same message, whether or not the account exists. The
    difference between "sent" and "no such user" tells an attacker which of a
    list of addresses are real, and this endpoint needs no authentication.

    For the same reason a mail failure is not surfaced either; it is logged and
    the caller still sees success.
    """
    same_answer = MessageResponse(
        message="If that address has an account, a reset link is on its way."
    )

    email = payload.email.strip().lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not user.is_active:
        # Deliberately silent. An inactive account should not be resettable
        # either -- that would be a way back in for someone who was switched off.
        print(f"[AUTH] password reset requested for unknown/inactive {email!r}")
        return same_answer

    # Any outstanding links are burned first. Otherwise asking twice leaves two
    # working tokens, and the older one usually ends up in the wrong hands.
    for old in session.exec(
        select(PasswordReset).where(
            PasswordReset.user_id == user.id, PasswordReset.used == False  # noqa: E712
        )
    ).all():
        old.used = True
        session.add(old)

    reset = PasswordReset(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=_utcnow() + _RESET_TTL,
    )
    session.add(reset)
    session.commit()

    from api.services import email_service

    if email_service.is_configured():
        result = email_service.send_password_reset(user.email, _reset_url(reset.token))
        if not result.sent:
            print(f"[AUTH] reset email to {user.email} failed: {result.error}")
    else:
        # Development: no SMTP configured, so put the link where the developer
        # can see it. Never happens in production, where SMTP is required.
        print(f"[AUTH] SMTP not configured — reset link: {_reset_url(reset.token)}")

    return same_answer


def _load_reset(session: Session, token: str) -> tuple[PasswordReset | None, str]:
    """The reset row for a token, plus why it cannot be used if it cannot."""
    reset = session.exec(select(PasswordReset).where(PasswordReset.token == token)).first()
    if not reset:
        return None, "This link is not valid."
    if reset.used:
        return None, "This link has already been used."
    if is_expired(reset.expires_at):
        return None, "This link has expired. Ask for a new one."
    return reset, ""


@router.get("/password-reset/{token}", response_model=PasswordResetValidateResponse)
def validate_password_reset(
    token: str,
    session: Annotated[Session, Depends(get_session)],
) -> PasswordResetValidateResponse:
    """Check a link before showing the form behind it.

    Unlike the request endpoint this does distinguish outcomes -- holding a
    token is already evidence, and "expired" and "invalid" need different
    responses from the person reading them.
    """
    reset, reason = _load_reset(session, token)
    if not reset:
        return PasswordResetValidateResponse(valid=False, reason=reason)
    user = session.get(User, reset.user_id)
    if not user or not user.is_active:
        return PasswordResetValidateResponse(valid=False, reason="This link is not valid.")
    return PasswordResetValidateResponse(valid=True, email=user.email)


@router.post("/password-reset/confirm", response_model=MessageResponse)
def confirm_password_reset(
    payload: PasswordResetConfirm,
    session: Annotated[Session, Depends(get_session)],
) -> MessageResponse:
    """Set the new password and burn the link."""
    reset, reason = _load_reset(session, payload.token)
    if not reset:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    user = session.get(User, reset.user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This link is not valid."
        )

    user.hashed_password = hash_password(payload.password)
    reset.used = True
    session.add(user)
    session.add(reset)

    # Every existing session goes. Someone resetting a password may be doing it
    # because someone else has one, and leaving those alive defeats the point.
    for token_row in session.exec(
        select(RefreshToken).where(RefreshToken.user_id == user.id)
    ).all():
        session.delete(token_row)

    session.commit()
    print(f"[AUTH] password reset completed for {user.email}")
    return MessageResponse(message="Your password has been changed. You can sign in now.")
