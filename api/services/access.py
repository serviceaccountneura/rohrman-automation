"""Who may do what, and which dealerships they may see.

TWO SEPARATE QUESTIONS
    Role decides what someone can DO: only an admin manages other people.
    Dealership assignment decides what someone can SEE: a clerk at one store
    has no business reading another store's invoices.

    They are independent. An admin is unrestricted on both by default, but a
    restricted admin is a coherent thing to want and is supported.

HOW ASSIGNMENT IS ENCODED
    `User.dealerships` is a JSON array of Tekion display names, and an EMPTY
    string means every dealership. Empty is the default, so existing accounts
    and anyone created before this existed keep the access they had.

    An empty ARRAY is deliberately NOT the same as an empty string. "[]" would
    mean "assigned to nothing", and treating that as "everything" would turn a
    UI bug -- a form that forgot to send its selection -- into silent access to
    all 19 stores. `set_dealerships` refuses to write one.
"""
from __future__ import annotations

import json

from fastapi import HTTPException, status

from api.models.db import User

ROLE_ADMIN = "ADMIN"


def is_admin(user: User) -> bool:
    """Admins manage users. Superusers are admins whatever their role says."""
    return bool(user.is_superuser) or str(user.role or "").upper() == ROLE_ADMIN


def require_admin(user: User) -> None:
    """Raise unless this user may manage other users."""
    if not is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can manage users.",
        )


def parse_dealerships(raw: str | None) -> list[str]:
    """The dealership names in a stored value. Empty list means unrestricted.

    Never raises on bad data: a row that cannot be parsed is treated as
    unrestricted, which matches what an empty value means and keeps a corrupt
    string from locking someone out of the application entirely.
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(name).strip() for name in parsed if str(name).strip()]


def encode_dealerships(names: list[str] | None) -> str:
    """Store a selection. `None` or an all-stores selection becomes "".

    Duplicates are dropped and order is preserved so the UI shows them back the
    way they were picked.
    """
    if not names:
        return ""
    seen: list[str] = []
    for name in names:
        clean = str(name).strip()
        if clean and clean not in seen:
            seen.append(clean)
    return json.dumps(seen) if seen else ""


def has_all_dealerships(user: User) -> bool:
    return not parse_dealerships(user.dealerships)


def visible_dealerships(user: User, dealers: list[dict]) -> list[dict]:
    """Narrow a Tekion dealer roster to what this user may see.

    Matching is case-insensitive on the display name, because that name is what
    the assignment stores and what Tekion returns -- but casing has drifted
    between the two before ("Oakbrook Toy. in Westmont").

    An assignment naming a dealership this login cannot reach simply matches
    nothing. That is the safe direction: it hides a store rather than inventing
    one.
    """
    allowed = parse_dealerships(user.dealerships)
    if not allowed:
        return dealers
    wanted = {name.lower() for name in allowed}
    return [d for d in dealers if str(d.get("name", "")).strip().lower() in wanted]


def may_access_dealership(user: User, dealership_name: str | None) -> bool:
    """Whether this user may act on one dealership by name."""
    allowed = parse_dealerships(user.dealerships)
    if not allowed:
        return True
    if not dealership_name:
        # No dealership named at all. Only someone unrestricted can mean "any".
        return False
    return dealership_name.strip().lower() in {n.lower() for n in allowed}


def require_dealership(user: User, dealership_name: str | None) -> None:
    """Raise unless this user may act on the named dealership."""
    if not may_access_dealership(user, dealership_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have access to {dealership_name or 'that dealership'}.",
        )
