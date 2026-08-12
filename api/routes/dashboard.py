"""Dashboard route — aggregated stats for the frontend dashboard page.

GET /api/dashboard  — returns summary counts, documents by PO type, and
                       the 3 most recent exceptions in a single response.
GET /api/dashboard/documents — paginated list of documents, filterable by po_type.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import Document
from api.models.schemas import (
    DashboardResponse,
    DashboardSummary,
    DocumentItem,
    DocumentListResponse,
    DocumentsByType,
    ExceptionAnalyticsResponse,
    ExceptionItem,
    ExceptionListItem,
    ExceptionListResponse,
    ExceptionTypeCount,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
def get_dashboard(
    session: Annotated[Session, Depends(get_session)],
) -> DashboardResponse:
    # Summary counts — single query per status.
    def _count(status: str) -> int:
        return session.exec(select(func.count()).where(Document.status == status)).one()

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


@router.get("/documents", response_model=DocumentListResponse)
def list_documents(
    session: Annotated[Session, Depends(get_session)],
    po_type: str | None = Query(
        default=None, description="Filter by PO type (SUBLET, MISCELLANEOUS, STOCK)"
    ),
    status: str | None = Query(
        default=None,
        description="Filter by status (PENDING, PROCESSED, EXCEPTION, AUTO_RESOLVED)",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> DocumentListResponse:
    """Paginated list of documents, optionally filtered by po_type and/or status."""
    query = select(Document)

    if po_type:
        query = query.where(Document.po_type == po_type)
    if status:
        query = query.where(Document.status == status)

    # Total count (before pagination)
    count_query = select(func.count()).select_from(Document)
    if po_type:
        count_query = count_query.where(Document.po_type == po_type)
    if status:
        count_query = count_query.where(Document.status == status)
    total = session.exec(count_query).one()

    # Paginated results
    offset = (page - 1) * page_size
    docs = session.exec(
        query.order_by(Document.created_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(page_size)
    ).all()

    items = [
        DocumentItem(
            id=doc.id,
            file_name=doc.file_name,
            dealership_name=doc.dealership_name,
            vendor_name=doc.vendor_name,
            invoice_number=doc.invoice_number,
            ro_number=doc.ro_number,
            po_number=doc.po_number,
            po_type=doc.po_type,
            status=doc.status,
            exception_type=doc.exception_type,
            created_at=doc.created_at,
            processed_at=doc.processed_at,
        )
        for doc in docs
    ]

    total_pages = (total + page_size - 1) // page_size

    return DocumentListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/exceptions", response_model=ExceptionListResponse)
def list_exceptions(
    session: Annotated[Session, Depends(get_session)],
    severity: str | None = Query(
        default=None, description="Filter by severity (HIGH, MEDIUM, LOW)"
    ),
    exception_type: str | None = Query(
        default=None,
        description="Filter by exception type (VENDOR_NOT_FOUND, PO_MISMATCH, etc.)",
    ),
    document_type: str | None = Query(
        default=None,
        description="Filter by document type (AP_INVOICE, PARTS_TICKET, etc.)",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ExceptionListResponse:
    """Paginated list of exception documents, optionally filtered by severity, exception_type, and/or document_type."""
    query = select(Document).where(Document.status == "EXCEPTION")

    if severity:
        query = query.where(Document.severity == severity)
    if exception_type:
        query = query.where(Document.exception_type == exception_type)
    if document_type:
        query = query.where(Document.document_type == document_type)

    # Total count
    count_query = (
        select(func.count()).select_from(Document).where(Document.status == "EXCEPTION")
    )
    if severity:
        count_query = count_query.where(Document.severity == severity)
    if exception_type:
        count_query = count_query.where(Document.exception_type == exception_type)
    if document_type:
        count_query = count_query.where(Document.document_type == document_type)
    total = session.exec(count_query).one()

    # Paginated results
    offset = (page - 1) * page_size
    docs = session.exec(
        query.order_by(Document.created_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(page_size)
    ).all()

    items = [
        ExceptionListItem(
            id=doc.id,
            vendor_name=doc.vendor_name,
            invoice_number=doc.invoice_number,
            vin=doc.vin,
            po_number=doc.po_number,
            ro_number=doc.ro_number,
            exception_type=doc.exception_type,
            severity=doc.severity,
            detected_on=doc.created_at,
            document_type=doc.document_type,
            status=doc.status,
        )
        for doc in docs
    ]

    total_pages = (total + page_size - 1) // page_size

    return ExceptionListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/exceptions/analytics", response_model=ExceptionAnalyticsResponse)
def exception_analytics(
    session: Annotated[Session, Depends(get_session)],
) -> ExceptionAnalyticsResponse:
    """Aggregated exception analytics — counts by severity and exception type."""
    # Total exceptions (status=EXCEPTION)
    total = session.exec(
        select(func.count()).where(Document.status == "EXCEPTION")
    ).one()

    # By severity
    def _sev_count(level: str) -> int:
        return session.exec(
            select(func.count()).where(
                Document.status == "EXCEPTION", Document.severity == level
            )
        ).one()

    # Auto-resolved count
    auto_resolved = session.exec(
        select(func.count()).where(Document.status == "AUTO_RESOLVED")
    ).one()

    # Breakdown by exception_type
    type_rows = session.exec(
        select(Document.exception_type, func.count())
        .where(Document.status == "EXCEPTION", Document.exception_type.isnot(None))
        .group_by(Document.exception_type)
        .order_by(func.count().desc())
    ).all()
    by_type = [
        ExceptionTypeCount(exception_type=t or "UNKNOWN", count=c) for t, c in type_rows
    ]

    return ExceptionAnalyticsResponse(
        total_exceptions=total,
        critical=_sev_count("HIGH"),
        medium=_sev_count("MEDIUM"),
        low=_sev_count("LOW"),
        auto_resolved=auto_resolved,
        by_exception_type=by_type,
    )
