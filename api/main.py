"""FastAPI app — single server for OCR + Tekion PO automation + auth.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()  # Load TEKION_* and other env vars before anything imports them.

# The Windows console defaults to cp1252, which cannot encode characters like
# "->" arrows or em dashes. A print() that raises inside a worker aborts the job
# mid-flight — one such line failed a run *after* it had already created a
# purchase order in Tekion, and the retry then created a second one. Logging
# must never be able to do that.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # not a real console (tests, pipes)
        pass

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from api.deps import get_current_user
from api.routes import (
    auth,
    dashboard,
    invoices,
    notifications,
    ocr,
    pipeline,
    tekion,
    users,
)
from api.services import worker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the document-queue workers alongside the API.

    Set PIPELINE_WORKERS=0 to disable them here (e.g. when running workers as a
    separate process).
    """
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(
    title="Rohrman Automation API",
    description=(
        "Folder-based invoice pipeline (OCR -> Tekion PO / journal entry) "
        "+ Tekion PO creation + JWT auth"
    ),
    version="1.1.0",
    lifespan=lifespan,
)

# Auth routes — public (signup/login/refresh). /me enforces auth internally.
app.include_router(auth.router)

# Protected routes — require a valid access token for every path operation.
app.include_router(ocr.router, dependencies=[Depends(get_current_user)])
app.include_router(pipeline.router, dependencies=[Depends(get_current_user)])
app.include_router(tekion.router, dependencies=[Depends(get_current_user)])
app.include_router(dashboard.router, dependencies=[Depends(get_current_user)])
app.include_router(invoices.router, dependencies=[Depends(get_current_user)])
app.include_router(users.router, dependencies=[Depends(get_current_user)])
app.include_router(notifications.router, dependencies=[Depends(get_current_user)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
