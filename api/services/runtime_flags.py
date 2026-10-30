"""Persisted runtime flags — TEKION_PO_WRITES / OEM_POST without redeploy.

Env values seed a key the first time it is read. After that the `app_settings`
row wins. A short process-local cache keeps hot paths cheap; PATCH clears it.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlmodel import Session

from api.config import settings
from api.db import engine
from api.models.db import AppSetting


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

KEY_TEKION_PO_WRITES = "tekion_po_writes"
KEY_OEM_POST = "oem_post"

_KNOWN_KEYS = (KEY_TEKION_PO_WRITES, KEY_OEM_POST)

# key → (value, monotonic_ts)
_cache: dict[str, tuple[bool, float]] = {}
_lock = threading.Lock()
_TTL_SEC = 2.0


def _env_default(key: str) -> bool:
    if key == KEY_TEKION_PO_WRITES:
        return bool(settings.tekion_po_writes)
    if key == KEY_OEM_POST:
        return bool(settings.oem_post)
    raise KeyError(f"unknown runtime flag: {key}")


def _read_or_seed(session: Session, key: str) -> bool:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=_env_default(key), updated_at=_utcnow())
        session.add(row)
        session.commit()
        session.refresh(row)
    return bool(row.value)


def get_flag(key: str, session: Session | None = None) -> bool:
    """Current value for a runtime flag (DB, seeded from env on first read)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[1] < _TTL_SEC:
            return hit[0]

    if session is not None:
        value = _read_or_seed(session, key)
    else:
        with Session(engine) as own:
            value = _read_or_seed(own, key)

    with _lock:
        _cache[key] = (value, time.monotonic())
    return value


def set_flag(
    session: Session,
    key: str,
    value: bool,
    *,
    updated_by: UUID | None = None,
) -> bool:
    """Persist a flag and drop the process cache so the next read is fresh."""
    if key not in _KNOWN_KEYS:
        raise KeyError(f"unknown runtime flag: {key}")
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    row.updated_at = _utcnow()
    row.updated_by = updated_by
    session.add(row)
    session.commit()
    session.refresh(row)
    clear_cache(key)
    return bool(row.value)


def clear_cache(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def tekion_po_writes(session: Session | None = None) -> bool:
    return get_flag(KEY_TEKION_PO_WRITES, session)


def oem_post(session: Session | None = None) -> bool:
    return get_flag(KEY_OEM_POST, session)


def all_flags(session: Session | None = None) -> dict[str, bool]:
    return {
        KEY_TEKION_PO_WRITES: tekion_po_writes(session),
        KEY_OEM_POST: oem_post(session),
    }
