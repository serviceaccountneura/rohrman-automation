"""Database engine + session dependency (SQLModel / SQLAlchemy)."""
from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, create_engine

from api.config import settings

# pool_pre_ping avoids stale connections after the DB restarts.
# Default pool (5 + overflow 10) is easy to exhaust locally: 2 pipeline
# workers + uvicorn reload + the FE polling queue/docs/exceptions every few
# seconds. Bump room so QueuePool timeouts are not the failure mode.
engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
    pool_timeout=60,
)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SessionDep = Session  # re-exported for Annotated[..., SessionDep] usage
