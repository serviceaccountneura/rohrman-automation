"""Dashboard route — aggregated stats for the frontend dashboard page.

GET /api/dashboard  — returns summary counts, documents by PO type, and
                       the 3 most recent exceptions in a single response.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import Document
from api.models.schemas import (
    DashboardResponse,
    DashboardSummary,
    DocumentsByType,
    ExceptionItem,
)

from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
def get_dashboard(session: Session = Depends(get_session)) -> DashboardResponse:
    # Summary counts — single query per status.
    def _count(status: str) -> int:
        return session.exec(
            select(func.count()).where(Document.status == status)
        ).one()

    summary = DashboardSummary(
        total=session.exec(select(func.count()).select_from(Document)).one(),
        processed=_count("PROCESSED"),
        exceptions=_count("EXCEPTION"),
        auto_resolved=_count("AUTO_RESOLVED"),
    )

    # Documents by PO type.
    rows = session.exec(
        select(Document.po_type, func.count())
        .where(Document.po_type != "")
        .group_by(Document.po_type)
    ).all()
    by_type = DocumentsByType()
    for po_type, count in rows:
        if hasattr(by_type, po_type):
            setattr(by_type, po_type, count)

    # Latest 3 exceptions.
    recent = session.exec(
        select(Document)
        .where(Document.status == "EXCEPTION")
        .order_by(Document.created_at.desc())  # type: ignore[union-attr]
        .limit(3)
    ).all()
    recent_exceptions = [
        ExceptionItem(
            id=doc.id,
            vendor=doc.vendor_name,
            invoice_number=doc.invoice_number,
            po_number=doc.po_number,
            exception_type=doc.exception_type or "UNKNOWN",
        )
        for doc in recent
    ]

    return DashboardResponse(
        summary=summary,
        by_type=by_type,
        recent_exceptions=recent_exceptions,
    )
