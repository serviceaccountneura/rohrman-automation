"""FastAPI app — single server for OCR + Tekion PO automation.
"""
from __future__ import annotations

from fastapi import FastAPI

from api.routes import ocr, tekion

app = FastAPI(
    title="Rohrman Automation API",
    description="OCR extraction + Tekion PO creation (sublet & misc)",
    version="1.0.0",
)

app.include_router(ocr.router)
app.include_router(tekion.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
