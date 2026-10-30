"""Admin Settings — runtime feature flags (TEKION_PO_WRITES / OEM_POST)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from api.db import get_session
from api.deps import CurrentUserDep
from api.services import access
from api.services import runtime_flags

router = APIRouter(prefix="/api/settings", tags=["settings"])


class RuntimeFlagsResponse(BaseModel):
    tekion_po_writes: bool = Field(alias="tekionPoWrites")
    oem_post: bool = Field(alias="oemPost")

    model_config = {"populate_by_name": True}


class RuntimeFlagsUpdate(BaseModel):
    tekion_po_writes: bool | None = Field(default=None, alias="tekionPoWrites")
    oem_post: bool | None = Field(default=None, alias="oemPost")

    model_config = {"populate_by_name": True}


@router.get("/runtime-flags", response_model=RuntimeFlagsResponse)
def get_runtime_flags(
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> RuntimeFlagsResponse:
    """Current Tekion write / OEM post switches. Admin only."""
    access.require_admin(current_user)
    return RuntimeFlagsResponse(
        tekionPoWrites=runtime_flags.tekion_po_writes(session),
        oemPost=runtime_flags.oem_post(session),
    )


@router.patch("/runtime-flags", response_model=RuntimeFlagsResponse)
def patch_runtime_flags(
    body: RuntimeFlagsUpdate,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
) -> RuntimeFlagsResponse:
    """Flip TEKION_PO_WRITES / OEM_POST without editing .env. Admin only."""
    access.require_admin(current_user)
    if body.tekion_po_writes is not None:
        runtime_flags.set_flag(
            session,
            runtime_flags.KEY_TEKION_PO_WRITES,
            body.tekion_po_writes,
            updated_by=current_user.id,
        )
    if body.oem_post is not None:
        runtime_flags.set_flag(
            session,
            runtime_flags.KEY_OEM_POST,
            body.oem_post,
            updated_by=current_user.id,
        )
    return RuntimeFlagsResponse(
        tekionPoWrites=runtime_flags.tekion_po_writes(session),
        oemPost=runtime_flags.oem_post(session),
    )
