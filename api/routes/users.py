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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from api.config import settings
from api.services import access, email_service
from api.services.security import hash_password, verify_password
from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import InviteCode, Notification, RefreshToken, User
from api.models.schemas import (
    CurrentUserResponse,
    PendingInviteItem,
    PendingInviteListResponse,
    UpdateMeRequest,
    CreateInviteRequest,
    InviteResponse,
    InviteValidateResponse,
    UpdateUserStatusRequest,
    UserListItem,
    UserListResponse,
)

# The two roles the UI offers. ADMIN can manage users; AP_CLERK is everyone
# else. Kept as the existing internal names so seeded accounts stay valid --
# the friendlier "Admin" / "User" wording lives in the frontend.
ROLE_ADMIN = "ADMIN"
ROLE_USER = "AP_CLERK"
ROLE_LABELS = {ROLE_ADMIN: "an administrator", ROLE_USER: "a user"}


router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=UserListResponse)
def list_users(
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> UserListResponse:
    """Every user, with name, email, role, dealerships and status.

    Administrators only. A regular user has no reason to see the roster, and
    exposing it would leak every colleague's email address.
    """
    access.require_admin(current_user)

    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    items = [
        UserListItem(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=u.role,
            dealerships=access.parse_dealerships(u.dealerships),
            status="ACTIVE" if u.is_active else "INACTIVE",
        )
        for u in users
    ]
    return UserListResponse(items=items, total=len(items))


@router.get("/me", response_model=CurrentUserResponse)
def read_me(current_user: CurrentUserDep) -> CurrentUserResponse:
    """The signed-in user, including what they are allowed to do and see.

    The frontend needs this to decide whether to offer user management at all.
    Hiding the controls is not the security boundary -- the routes enforce that
    -- but showing someone a button that always fails is its own bug.
    """
    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        is_admin=access.is_admin(current_user),
        dealerships=access.parse_dealerships(current_user.dealerships),
        all_dealerships=access.has_all_dealerships(current_user),
    )


@router.patch("/me", response_model=CurrentUserResponse)
def update_me(
    req: UpdateMeRequest,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> CurrentUserResponse:
    """Edit your own details.

    Deliberately narrow: a name, and a password when the current one is given.
    Role and dealership assignment are NOT editable here -- letting someone
    change their own role or reach is the whole point of having an admin.
    """
    if req.full_name is not None:
        current_user.full_name = req.full_name.strip() or None

    if req.new_password:
        if not req.current_password or not verify_password(
            req.current_password, current_user.hashed_password
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your current password is not correct.",
            )
        current_user.hashed_password = hash_password(req.new_password)

    current_user.updated_at = datetime.now(timezone.utc)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        is_admin=access.is_admin(current_user),
        dealerships=access.parse_dealerships(current_user.dealerships),
        all_dealerships=access.has_all_dealerships(current_user),
    )


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> dict:
    """Delete a user by ID. Administrators only; cannot delete yourself."""
    access.require_admin(current_user)

    if str(current_user.id) == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Three tables point at users, and Postgres refuses the delete while any of
    # them still does. Each one gets a different answer rather than a blanket
    # cascade, because they do not all mean the same thing.

    # Sessions die with the account -- a deleted user must not keep a working
    # refresh token.
    for token in session.exec(
        select(RefreshToken).where(RefreshToken.user_id == user.id)
    ).all():
        session.delete(token)

    # Notifications were addressed to this person and mean nothing without them.
    for note in session.exec(
        select(Notification).where(Notification.user_id == user.id)
    ).all():
        session.delete(note)

    # Invites are kept: who was invited, when, and whether it was taken up is
    # worth having after the admin who sent it has gone. The references to this
    # user are nulled instead.
    for invite in session.exec(
        select(InviteCode).where(InviteCode.created_by == user.id)
    ).all():
        if invite.used:
            invite.created_by = None
            session.add(invite)
        else:
            # An unused invite is a live link. Removing the person who sent it
            # should not leave a way into the system that nobody owns.
            session.delete(invite)

    for invite in session.exec(
        select(InviteCode).where(InviteCode.used_by == user.id)
    ).all():
        invite.used_by = None
        session.add(invite)

    session.delete(user)
    session.commit()
    return {"message": "User deleted", "user_id": user_id}


@router.patch("/{user_id}")
def update_user_status(
    user_id: str,
    req: UpdateUserStatusRequest,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> dict:
    """Activate or deactivate a user. Administrators only."""
    access.require_admin(current_user)

    if str(current_user.id) == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot disable your own sign-in.",
        )

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


@router.get("/invites", response_model=PendingInviteListResponse)
def list_pending_invites(
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> PendingInviteListResponse:
    """Invitations sent but not yet taken up.

    Administrators only, same as the user roster. Expired invitations are
    included and flagged rather than dropped -- an invitation that quietly
    disappears looks like it was never sent, and "it expired" is the answer to
    why somebody never got in.
    """
    access.require_admin(current_user)

    now = datetime.now(timezone.utc)
    invites = session.exec(
        select(InviteCode)
        .where(InviteCode.used == False)  # noqa: E712
        .order_by(InviteCode.created_at.desc())  # type: ignore[union-attr]
    ).all()

    # Resolve inviter names in one pass rather than a query per row.
    inviter_ids = {i.created_by for i in invites if i.created_by}
    inviters: dict = {}
    if inviter_ids:
        for u in session.exec(select(User).where(User.id.in_(inviter_ids))).all():  # type: ignore[union-attr]
            inviters[u.id] = u.full_name or u.email

    items = []
    for invite in invites:
        expires = invite.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        items.append(
            PendingInviteItem(
                id=invite.id,
                email=invite.email,
                role=invite.role,
                full_name=invite.full_name,
                dealerships=access.parse_dealerships(invite.dealerships),
                invited_by=inviters.get(invite.created_by, "") if invite.created_by else "",
                created_at=invite.created_at,
                expires_at=invite.expires_at,
                expired=expires < now,
            )
        )
    return PendingInviteListResponse(items=items, total=len(items))


@router.post("/invites/{invite_id}/resend", response_model=InviteResponse)
def resend_invite(
    invite_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> InviteResponse:
    """Send an invitation again, on a fresh link.

    A new code is issued rather than the old one re-sent, and the old one is
    retired. That resets the 24-hour window -- which is the point of resending,
    since the usual reason is that the first one expired -- and means a link
    forwarded from the original email stops working.

    The role and dealerships are carried over unchanged. Resending is not a
    place to quietly widen somebody's access.
    """
    access.require_admin(current_user)

    invite = session.get(InviteCode, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invite.used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{invite.email} has already signed up.",
        )

    # An admin cannot resend access they do not themselves have -- the original
    # may have been sent by someone with a wider reach.
    for name in access.parse_dealerships(invite.dealerships):
        access.require_dealership(current_user, name)

    invite.used = True  # retire the old link
    session.add(invite)

    code = secrets.token_urlsafe(16)[:24]
    fresh = InviteCode(
        code=code,
        email=invite.email,
        role=invite.role,
        full_name=invite.full_name,
        dealerships=invite.dealerships,
        created_by=current_user.id,
    )
    session.add(fresh)
    session.commit()
    session.refresh(fresh)

    invite_url = f"{settings.frontend_url}/invite?code={code}"
    result = email_service.send_invite(
        to_email=invite.email,
        invite_url=invite_url,
        role_label=ROLE_LABELS.get(invite.role, invite.role),
        invited_by=current_user.full_name or current_user.email,
    )
    if not result.sent:
        print(f"[USERS] resend for {invite.email} not emailed: {result.detail}")

    return InviteResponse(
        invite_code=code,
        invite_url=invite_url,
        email=invite.email,
        role=invite.role,
        full_name=invite.full_name,
        dealerships=access.parse_dealerships(invite.dealerships),
        expires_at=fresh.expires_at,
        email_sent=result.sent,
        email_error=result.detail,
    )


@router.delete("/invites/{invite_id}")
def revoke_invite(
    invite_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> dict:
    """Cancel an invitation that has not been used.

    Without this a mistaken invite stays live for 24 hours with no way to stop
    it, which for an ADMIN invitation is worth being able to undo immediately.
    """
    access.require_admin(current_user)

    invite = session.get(InviteCode, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invite.used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That invitation has already been used.",
        )

    session.delete(invite)
    session.commit()
    return {"message": "Invitation revoked", "email": invite.email}


@router.post("/invite", response_model=InviteResponse)
def create_invite(
    req: CreateInviteRequest,
    current_user: CurrentUserDep,
    session: Annotated[Session, Depends(get_session)],
) -> InviteResponse:
    """Invite someone by email. Single-use link, valid for 24 hours.

    Administrators only. An admin may invite another admin, so the group can
    grow without a single person becoming a bottleneck; a regular user cannot
    invite anyone at all.

    The link is emailed when SMTP is configured, and always returned either
    way -- a mail server that is unset or refusing should not stop an admin
    adding somebody, so the response carries the link for them to pass on.
    """
    access.require_admin(current_user)

    # An admin who is themselves restricted cannot hand out access they do not
    # have. Without this, a single-store admin could invite someone to all 19.
    requested = list(req.dealerships or [])
    if req.dealerships is not None and not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one dealership, or choose all dealerships.",
        )
    if requested:
        for name in requested:
            access.require_dealership(current_user, name)
    elif not access.has_all_dealerships(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only invite someone to the dealerships you have access to.",
        )

    email = req.email.strip().lower()

    existing = session.exec(select(User).where(User.email == email)).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{email} already has an account.",
        )

    # Supersede any invite already outstanding for this address, so a resend
    # cannot leave two live links to the same account.
    for stale in session.exec(
        select(InviteCode).where(InviteCode.email == email, InviteCode.used == False)  # noqa: E712
    ).all():
        stale.used = True
        session.add(stale)

    code = secrets.token_urlsafe(16)[:24]
    invite = InviteCode(
        code=code,
        email=email,
        role=req.role,
        full_name=req.full_name,
        dealerships=access.encode_dealerships(requested),
        created_by=current_user.id,
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)

    invite_url = f"{settings.frontend_url}/invite?code={code}"

    result = email_service.send_invite(
        to_email=email,
        invite_url=invite_url,
        role_label=ROLE_LABELS.get(req.role, req.role),
        invited_by=current_user.full_name or current_user.email,
    )
    if not result.sent:
        print(f"[USERS] invite for {email} created but not emailed: {result.detail}")

    return InviteResponse(
        invite_code=code,
        invite_url=invite_url,
        email=email,
        role=req.role,
        full_name=req.full_name,
        dealerships=access.parse_dealerships(invite.dealerships),
        expires_at=invite.expires_at,
        email_sent=result.sent,
        email_error=result.detail,
    )
