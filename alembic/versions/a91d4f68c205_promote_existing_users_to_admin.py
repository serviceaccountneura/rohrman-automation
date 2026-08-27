"""promote existing users to admin

User management had no permission check: every signed-in account could invite,
delete and deactivate anyone. Adding an admin-only guard therefore removes a
power everybody already had, and on any existing database that means NOBODY can
manage users any more -- a lockout with no route back through the UI.

Promoting the accounts that exist when this runs preserves exactly what they
could already do. It grants nothing new.

Accounts created after this point default to AP_CLERK and must be invited as
administrators explicitly.

Revision ID: a91d4f68c205
Revises: f5a2c8e13b47
"""
from __future__ import annotations

from alembic import op

revision = "a91d4f68c205"
down_revision = "f5a2c8e13b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET role = 'ADMIN' WHERE role = 'AP_CLERK'")


def downgrade() -> None:
    # Not reversible: which accounts were promoted here is not recorded, and
    # demoting every admin would lock the application again.
    pass
