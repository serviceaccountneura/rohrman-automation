"""Auth routes — signup, login, refresh, me.

POST   /api/auth/signup   — register a new user
POST   /api/auth/login    — authenticate, returns access + refresh tokens
POST   /api/auth/refresh  — exchange a refresh token for a new token pair
POST   /api/auth/logout   — revoke the supplied refresh token
GET    /api/auth/me       — return the current authenticated user
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from api.config import settings
from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import RefreshToken, User
from api.models.schemas import (
    MessageResponse,
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

    user = User(
        email=req.email,
        full_name=req.full_name,
        hashed_password=hash_password(req.password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


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
