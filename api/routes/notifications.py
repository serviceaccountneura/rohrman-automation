"""Notification routes — list, mark as read, delete.

GET    /api/notifications              — paginated list for current user
POST   /api/notifications/{id}/read    — mark a single notification as read
POST   /api/notifications/read-all     — mark all as read
DELETE /api/notifications/{id}         — delete a notification
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from api.db import get_session
from api.deps import CurrentUserDep
from api.models.db import Notification, User
from api.models.schemas import NotificationItem, NotificationListResponse

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    current_user: CurrentUserDep,
    session: Annotated[Session, Depends(get_session)],
    is_read: bool | None = Query(default=None, description="Filter by read status"),
    category: str | None = Query(default=None, description="Filter by category"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> NotificationListResponse:
    """Paginated list of notifications for the current user."""
    base_filter = Notification.user_id == current_user.id

    query = select(Notification).where(base_filter)
    if is_read is not None:
        query = query.where(Notification.is_read == is_read)
    if category:
        query = query.where(Notification.category == category)

    # Total count
    count_query = select(func.count()).select_from(Notification).where(base_filter)
    if is_read is not None:
        count_query = count_query.where(Notification.is_read == is_read)
    if category:
        count_query = count_query.where(Notification.category == category)
    total = session.exec(count_query).one()

    # Unread count (always, regardless of filter)
    unread = session.exec(
        select(func.count())
        .select_from(Notification)
        .where(base_filter, Notification.is_read == False)
    ).one()

    # Paginated results
    offset = (page - 1) * page_size
    notifs = session.exec(
        query.order_by(Notification.created_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(page_size)
    ).all()

    items = [
        NotificationItem(
            id=n.id,
            title=n.title,
            message=n.message,
            category=n.category,
            severity=n.severity,
            is_read=n.is_read,
            document_id=n.document_id,
            created_at=n.created_at,
        )
        for n in notifs
    ]

    total_pages = (total + page_size - 1) // page_size

    return NotificationListResponse(
        items=items,
        total=total,
        unread_count=unread,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post("/{notification_id}/read")
def mark_as_read(
    notification_id: str,
    current_user: CurrentUserDep,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Mark a single notification as read."""
    notif = session.get(Notification, notification_id)
    if not notif or notif.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")

    if not notif.is_read:
        notif.is_read = True
        session.add(notif)
        session.commit()
    return {"message": "Notification marked as read", "id": notification_id}


@router.post("/read-all")
def mark_all_as_read(
    current_user: CurrentUserDep,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Mark all unread notifications as read for the current user."""
    notifs = session.exec(
        select(Notification).where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,
        )
    ).all()
    count = 0
    for n in notifs:
        n.is_read = True
        session.add(n)
        count += 1
    session.commit()
    return {"message": f"Marked {count} notifications as read", "count": count}


@router.delete("/{notification_id}")
def delete_notification(
    notification_id: str,
    current_user: CurrentUserDep,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Delete a notification."""
    notif = session.get(Notification, notification_id)
    if not notif or notif.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")

    session.delete(notif)
    session.commit()
    return {"message": "Notification deleted", "id": notification_id}
