"""Create (or update) a login user.

    uv run python scripts/seed_user.py
    uv run python scripts/seed_user.py --email me@ccript.com --password hunter2222
    uv run python scripts/seed_user.py --email admin@ccript.com --role ADMIN --superuser

Idempotent: running it again for the same email resets that user's password and
role rather than failing, which is what you want when you have forgotten the
password you seeded last week.

The API has no open registration — signup expects an invite code — so this is
the intended way to get the first account onto a fresh database.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from sqlmodel import Session, select  # noqa: E402

from api.db import engine  # noqa: E402
from api.models.db import User  # noqa: E402
from api.services.security import hash_password  # noqa: E402

DEFAULT_EMAIL = "test@ccript.com"
DEFAULT_PASSWORD = "testpass1234"


def main() -> int:
    ap = argparse.ArgumentParser(description="Create or update a login user")
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--name", default="Test User")
    ap.add_argument("--role", default="AP_CLERK", help="AP_CLERK | ADMIN | ...")
    ap.add_argument(
        "--superuser", action="store_true", help="Grant superuser privileges"
    )
    args = ap.parse_args()

    if len(args.password) < 8:
        print("Password must be at least 8 characters (the API enforces this).")
        return 1

    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == args.email)).first()

        if existing:
            existing.hashed_password = hash_password(args.password)
            existing.full_name = args.name
            existing.role = args.role
            existing.is_superuser = args.superuser
            existing.is_active = True
            session.add(existing)
            session.commit()
            print(f"Updated existing user {args.email}")
        else:
            session.add(
                User(
                    email=args.email,
                    full_name=args.name,
                    hashed_password=hash_password(args.password),
                    role=args.role,
                    is_superuser=args.superuser,
                    is_active=True,
                )
            )
            session.commit()
            print(f"Created user {args.email}")

    print()
    print("  Sign in at http://localhost:3000/login")
    print(f"    email    : {args.email}")
    print(f"    password : {args.password}")
    print(f"    role     : {args.role}{' (superuser)' if args.superuser else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
