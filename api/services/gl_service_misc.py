"""Misc PO GL resolution — fetches real GL accounts from Tekion per dealership,
caches in DB, and uses the LLM to match line item descriptions to GL account names.

Flow:
  1. Get GL accounts for the dealership (DB cache → Tekion fetch if missing)
  2. Build LLM prompt with line items + all GL account names
  3. LLM picks the best matching GL account name
  4. Map name back to full account ID (e.g. "1708_7473")
  5. Return the full account ID
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from api.models.db import TekionGlAccount
from api.services.tekion_client import TekionApiClient

# ── Vertex AI setup (reuse the OCR pipeline's client) ─────────────────────────

MAIN_PIPELINE = Path(__file__).resolve().parent.parent.parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

PROJECT_ROOT = MAIN_PIPELINE.parent
if not os.environ.get("VERTEX_CREDENTIALS"):
    creds = PROJECT_ROOT / "neura_vertex_ai.json"
    if creds.exists():
        os.environ["VERTEX_CREDENTIALS"] = str(creds)

from google.genai import types  # noqa: E402
from pipeline import get_client  # noqa: E402

GL_CLASSIFIER_MODEL = "gemini-2.5-flash-lite"

# ── DB cache: get or fetch GL accounts ─────────────────────────────────────────

def get_cached_gl_accounts(dealer_id: str, session: Session) -> list[TekionGlAccount]:
    """Get GL accounts from DB cache for a dealership."""
    return list(session.exec(
        select(TekionGlAccount).where(TekionGlAccount.dealer_id == dealer_id)
    ))


def save_gl_accounts(
    dealer_id: str,
    accounts: list[dict[str, Any]],
    session: Session,
) -> None:
    """Replace cached GL accounts for a dealership with fresh data from Tekion."""
    # Delete existing
    existing = session.exec(
        select(TekionGlAccount).where(TekionGlAccount.dealer_id == dealer_id)
    ).all()
    for row in existing:
        session.delete(row)

    # Insert new
    for acc in accounts:
        session.add(TekionGlAccount(
            id=uuid4(),
            dealer_id=dealer_id,
            account_id=acc["account_id"],
            account_number=acc["account_number"],
            account_name=acc["account_name"],
            account_type=acc.get("account_type", ""),
            department_type=acc.get("department_type", ""),
            active=acc.get("active", True),
        ))
    session.commit()
    print(f"[GL-Misc] Saved {len(accounts)} GL accounts for dealer {dealer_id}")


def refresh_gl_accounts(
    dealer_id: str,
    client: TekionApiClient,
    session: Session,
) -> list[TekionGlAccount]:
    """Force-refresh GL accounts from Tekion and update the DB cache."""
    client.switch_dealer(dealer_id)
    accounts = client.fetch_gl_accounts()
    save_gl_accounts(dealer_id, accounts, session)
    return get_cached_gl_accounts(dealer_id, session)


def get_or_fetch_gl_accounts(
    dealer_id: str,
    client: TekionApiClient,
    session: Session,
) -> list[TekionGlAccount]:
    """Get GL accounts from DB cache, or fetch from Tekion if not cached."""
    cached = get_cached_gl_accounts(dealer_id, session)
    if cached:
        return cached
    return refresh_gl_accounts(dealer_id, client, session)


# ── LLM classification ────────────────────────────────────────────────────────

def _build_prompt(line_descriptions: list[str], gl_accounts: list[TekionGlAccount]) -> str:
    """Build the LLM prompt with line items and available GL account names."""
    items_text = "\n".join(f"- {d}" for d in line_descriptions)

    # Build the GL account list — only active accounts
    accounts_text = "\n".join(
        f"- {acc.account_name}"
        for acc in gl_accounts
        if acc.active
    )

    return f"""You are an AP Accounting Classifier for an Automotive Dealership.

You are processing a Miscellaneous Purchase Order. Select the single best GL account for these line items.

LINE ITEMS:
{items_text}

AVAILABLE GL ACCOUNTS (pick exactly one):
{accounts_text}

Respond with ONLY the exact GL account name from the list above. No other text, no explanation."""


def _llm_pick_gl_account(
    line_descriptions: list[str],
    gl_accounts: list[TekionGlAccount],
) -> str | None:
    """Ask the LLM to pick the best GL account name for the line items.

    Returns the account name, or None if the LLM fails or returns an invalid name.
    """
    if not gl_accounts:
        return None

    prompt = _build_prompt(line_descriptions, gl_accounts)

    try:
        client = get_client()
        resp = client.models.generate_content(
            model=GL_CLASSIFIER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=100,
            ),
        )
        answer = (resp.text or "").strip()

        # Match the LLM's answer to an actual account name (fuzzy — the LLM
        # might add quotes, trailing periods, or slight variations).
        answer_upper = answer.upper().strip('"').strip("'").rstrip(".")
        for acc in gl_accounts:
            if acc.account_name.upper() == answer_upper:
                return acc.account_name
        # Fallback: check if the answer is contained in an account name or vice versa
        for acc in gl_accounts:
            name_upper = acc.account_name.upper()
            if answer_upper in name_upper or name_upper in answer_upper:
                return acc.account_name

        print(f"[GL-Misc] LLM returned unknown account name: '{answer}'")
        return None
    except Exception as e:
        print(f"[GL-Misc] LLM classification failed ({e})")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_misc_gl(
    dealership_name: str,
    dealer_id: str,
    line_descriptions: list[str],
    client: TekionApiClient,
    session: Session,
) -> str:
    """Resolve the GL account ID for a miscellaneous PO.

    1. Get GL accounts for the dealership (DB cache → Tekion fetch)
    2. LLM picks the best GL account name from the list
    3. Map name → full account ID (e.g. "1708_7473")

    Returns the full GL account ID string.
    Raises Exception if no GL accounts are available or LLM fails.
    """
    gl_accounts = get_or_fetch_gl_accounts(dealer_id, client, session)
    if not gl_accounts:
        raise Exception(f"No GL accounts found for dealer {dealer_id}. Try refreshing.")

    account_name = _llm_pick_gl_account(line_descriptions, gl_accounts)
    if not account_name:
        raise Exception(
            f"LLM could not match line items to a GL account for dealer {dealer_id}. "
            f"Items: {line_descriptions}"
        )

    # Find the full account ID
    for acc in gl_accounts:
        if acc.account_name == account_name:
            print(f"[GL-Misc] Resolved '{account_name}' → {acc.account_id}")
            return acc.account_id

    raise Exception(f"GL account '{account_name}' not found in cache for dealer {dealer_id}")
