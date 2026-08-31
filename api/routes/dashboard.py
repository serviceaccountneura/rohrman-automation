"""Dashboard route — aggregated stats for the frontend dashboard page.

GET /api/dashboard  — returns summary counts, documents by PO type, and
                       the 3 most recent exceptions in a single response.
GET /api/dashboard/documents — paginated list of documents, filterable by po_type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from api.db import get_session
from api.models.db import Document
from api.models.schemas import (
    TrendPoint,
    TrendsResponse,
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



# The dealership filter every endpoint here accepts. Kept as one helper so the
# summary, the tables and the charts can never disagree about which documents
# belong to a store -- a mismatch would show a total that its own breakdown
# contradicts.
#
# Matching is exact but case-insensitive. Tekion's display name is what the
# frontend stores and what the pipeline writes, so an exact match is right; a
# LIKE would let "Schaumburg Honda" quietly pull in "Schaumburg Honda Used".
DealershipQuery = Query(
    default=None,
    description="Only documents uploaded for this dealership.",
)


def _for_dealership(query, dealership):
    """Narrow a query to one dealership, or leave it alone when none is given."""
    if not dealership:
        return query
    return query.where(
        func.lower(Document.dealership_name) == dealership.strip().lower()
    )


@router.get("", response_model=DashboardResponse)
def get_dashboard(
    session: Annotated[Session, Depends(get_session)],
    dealership_name: str | None = DealershipQuery,
) -> DashboardResponse:
    # Summary counts — single query per status.
    def _count(status: str) -> int:
        return session.exec(
            _for_dealership(
                select(func.count())
                .select_from(Document)
                .where(Document.status == status),
                dealership_name,
            )
        ).one()

    summary = DashboardSummary(
        total=session.exec(
            _for_dealership(select(func.count()).select_from(Document), dealership_name)
        ).one(),
        processed=_count("PROCESSED"),
        exceptions=_count("EXCEPTION"),
        auto_resolved=_count("AUTO_RESOLVED"),
    )

    # Documents by PO type.
    rows = session.exec(
        _for_dealership(
            select(Document.po_type, func.count()).where(Document.po_type != ""),
            dealership_name,
        ).group_by(Document.po_type)
    ).all()
    by_type = DocumentsByType()
    for po_type, count in rows:
        if hasattr(by_type, po_type):
            setattr(by_type, po_type, count)

    # Latest 3 exceptions.
    recent = session.exec(
        _for_dealership(
            select(Document).where(Document.status == "EXCEPTION"), dealership_name
        )
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
    dealership_name: str | None = DealershipQuery,
) -> DocumentListResponse:
    """Paginated list of documents, optionally filtered by po_type and/or status."""
    query = _for_dealership(select(Document), dealership_name)

    if po_type:
        query = query.where(Document.po_type == po_type)
    if status:
        query = query.where(Document.status == status)

    # Total count (before pagination)
    count_query = _for_dealership(
        select(func.count()).select_from(Document), dealership_name
    )
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
            duplicate_of=doc.duplicate_of,
            file_name=doc.file_name,
            dealership_name=doc.dealership_name,
            vendor_name=doc.vendor_name,
            invoice_number=doc.invoice_number,
            vin=doc.vin,
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
    dealership_name: str | None = DealershipQuery,
) -> ExceptionListResponse:
    """Paginated list of exception documents, optionally filtered by severity, exception_type, and/or document_type."""
    query = _for_dealership(
        select(Document).where(Document.status == "EXCEPTION"), dealership_name
    )

    if severity:
        query = query.where(Document.severity == severity)
    if exception_type:
        query = query.where(Document.exception_type == exception_type)
    if document_type:
        query = query.where(Document.document_type == document_type)

    # Total count
    count_query = _for_dealership(
        select(func.count())
        .select_from(Document)
        .where(Document.status == "EXCEPTION"),
        dealership_name,
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
    dealership_name: str | None = DealershipQuery,
) -> ExceptionAnalyticsResponse:
    """Aggregated exception analytics — counts by severity and exception type."""
    # Total exceptions (status=EXCEPTION)
    total = session.exec(
        _for_dealership(
            select(func.count())
            .select_from(Document)
            .where(Document.status == "EXCEPTION"),
            dealership_name,
        )
    ).one()

    # By severity
    def _sev_count(level: str) -> int:
        return session.exec(
            _for_dealership(
                select(func.count())
                .select_from(Document)
                .where(Document.status == "EXCEPTION", Document.severity == level),
                dealership_name,
            )
        ).one()

    # Auto-resolved count
    auto_resolved = session.exec(
        _for_dealership(
            select(func.count())
            .select_from(Document)
            .where(Document.status == "AUTO_RESOLVED"),
            dealership_name,
        )
    ).one()

    # Breakdown by exception_type
    type_rows = session.exec(
        _for_dealership(
            select(Document.exception_type, func.count()).where(
                Document.status == "EXCEPTION", Document.exception_type.isnot(None)
            ),
            dealership_name,
        )
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


_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@router.get("/trends", response_model=TrendsResponse)
def document_trends(
    session: Annotated[Session, Depends(get_session)],
    months: int = Query(default=6, ge=1, le=24, description="How many months back."),
    dealership_name: str | None = DealershipQuery,
) -> TrendsResponse:
    """Documents per month, split by how they ended up.

    Drives the processing-trend chart. Every month in the window is returned
    even when nothing was processed in it: a chart that silently skips empty
    months compresses the gap and reads as steady throughput.
    """
    now = datetime.now(timezone.utc)

    # Walk back `months` whole months, then take everything from the 1st of the
    # earliest one. Done on the year/month numbers rather than by subtracting
    # days, which drifts across months of different lengths.
    year, month = now.year, now.month
    for _ in range(months - 1):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    window_start = datetime(year, month, 1, tzinfo=timezone.utc)

    rows = session.exec(
        _for_dealership(
            select(Document.created_at, Document.status).where(
                Document.created_at >= window_start
            ),
            dealership_name,
        )
    ).all()

    # Seed every bucket first so quiet months appear as zero rather than absent.
    buckets: dict[tuple[int, int], dict[str, int]] = {}
    cursor_year, cursor_month = year, month
    for _ in range(months):
        buckets[(cursor_year, cursor_month)] = {
            "total": 0,
            "processed": 0,
            "exceptions": 0,
            "auto_resolved": 0,
        }
        cursor_month += 1
        if cursor_month == 13:
            cursor_month = 1
            cursor_year += 1

    for created_at, status in rows:
        key = (created_at.year, created_at.month)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        if status == "PROCESSED":
            bucket["processed"] += 1
        elif status == "EXCEPTION":
            bucket["exceptions"] += 1
        elif status == "AUTO_RESOLVED":
            bucket["auto_resolved"] += 1

    points = [
        TrendPoint(
            month=_MONTH_NAMES[m - 1],
            year=y,
            month_number=m,
            total=counts["total"],
            processed=counts["processed"],
            exceptions=counts["exceptions"],
            auto_resolved=counts["auto_resolved"],
        )
        for (y, m), counts in sorted(buckets.items())
    ]

    return TrendsResponse(
        points=points,
        total=sum(p.total for p in points),
        processed=sum(p.processed for p in points),
        exceptions=sum(p.exceptions for p in points),
        auto_resolved=sum(p.auto_resolved for p in points),
    )
