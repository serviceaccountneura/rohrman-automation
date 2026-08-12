"""User management routes — list, delete, activate/deactivate.

GET    /api/users           — list all users (name, email, role, status)
DELETE /api/users/{user_id} — delete a user
PATCH  /api/users/{user_id} — activate or deactivate a user
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import User
from api.models.schemas import UpdateUserStatusRequest, UserListItem, UserListResponse

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
