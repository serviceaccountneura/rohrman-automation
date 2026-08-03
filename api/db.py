"""Database engine + session dependency (SQLModel / SQLAlchemy)."""
from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, create_engine

from api.config import settings

# pool_pre_ping avoids stale connections after the DB restarts.
engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SessionDep = Session  # re-exported for Annotated[..., SessionDep] usage
