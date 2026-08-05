"""FastAPI app — single server for OCR + Tekion PO automation + auth.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # Load TEKION_* and other env vars before anything imports them.

from fastapi import Depends, FastAPI

from api.deps import get_current_user
from api.routes import auth, ocr, tekion

app = FastAPI(
    title="Rohrman Automation API",
    description="OCR extraction + Tekion PO creation (sublet & misc) + JWT auth",
    version="1.0.0",
)

# Auth routes — public (signup/login/refresh). /me enforces auth internally.
app.include_router(auth.router)

# Protected routes — require a valid access token for every path operation.
app.include_router(ocr.router, dependencies=[Depends(get_current_user)])
app.include_router(tekion.router, dependencies=[Depends(get_current_user)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
