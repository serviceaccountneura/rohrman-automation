"""User management routes — list, delete, activate/deactivate, invite.

GET    /api/users              — list all users (name, email, role, status)
DELETE /api/users/{user_id}    — delete a user
PATCH  /api/users/{user_id}    — activate or deactivate a user
POST   /api/users/invite       — create an invite link (24h, single-use)
GET    /api/users/invite/{code} — validate an invite code + pre-fill fields
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.config import settings
from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import InviteCode, User
from api.models.schemas import (
    CreateInviteRequest,
    InviteResponse,
    InviteValidateResponse,
    UpdateUserStatusRequest,
    UserListItem,
    UserListResponse,
)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=UserListResponse)
def list_users(
    session: Annotated[Session, Depends(get_session)],
    _current_user: Annotated[User, Depends(CurrentUserDep)],
) -> UserListResponse:
    """List all users with name, email, role, and status."""
    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    items = [
        UserListItem(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=u.role,
            status="ACTIVE" if u.is_active else "INACTIVE",
        )
        for u in users
    ]
    return UserListResponse(items=items, total=len(items))


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(CurrentUserDep)],
) -> dict:
    """Delete a user by ID. Cannot delete yourself."""
    if str(current_user.id) == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    session.delete(user)
    session.commit()
    return {"message": "User deleted", "user_id": user_id}


@router.patch("/{user_id}")
def update_user_status(
    user_id: str,
    req: UpdateUserStatusRequest,
    session: Annotated[Session, Depends(get_session)],
    _current_user: Annotated[User, Depends(CurrentUserDep)],
) -> dict:
    """Activate or deactivate a user."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = req.is_active
    session.add(user)
    session.commit()
    return {
        "message": "User status updated",
        "user_id": user_id,
        "is_active": req.is_active,
    }


@router.post("/invite", response_model=InviteResponse)
def create_invite(
    req: CreateInviteRequest,
    current_user: Annotated[User, Depends(CurrentUserDep)],
    session: Annotated[Session, Depends(get_session)],
) -> InviteResponse:
    """Create a single-use invite link valid for 24 hours."""
    code = secrets.token_urlsafe(16)[:24]
    invite = InviteCode(
        code=code,
        role=req.role,
        full_name=req.full_name,
        created_by=current_user.id,
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)

    invite_url = f"{settings.frontend_url}/invite?code={code}"

    return InviteResponse(
        invite_code=code,
        invite_url=invite_url,
        role=req.role,
        full_name=req.full_name,
        expires_at=invite.expires_at,
    )
