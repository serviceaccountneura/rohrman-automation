"""Shared FastAPI dependencies — currently the authenticated user."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import User
from api.services.security import decode_token

# tokenUrl points at the login endpoint for OpenAPI's "Authorize" button.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise credentials_exc from e

    if payload.get("type") != "access":
        raise credentials_exc

    user_id_raw = payload.get("sub")
    if not user_id_raw:
        raise credentials_exc

    try:
        user_id = UUID(user_id_raw)
    except ValueError as e:
        raise credentials_exc from e

    user = session.exec(select(User).where(User.id == user_id)).first()
    if user is None:
        raise credentials_exc
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
