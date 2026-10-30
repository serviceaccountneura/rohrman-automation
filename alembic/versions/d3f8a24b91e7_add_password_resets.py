"""add password_resets

Backs "Forgot Password". A row per request: single-use, short-lived, and tied
to one account.

Deliberately its own table rather than columns on `users`. A reset token is a
credential with its own lifetime, and keeping it beside the password hash makes
it easy to leak one while reading the other.

Revision ID: d3f8a24b91e7
Revises: b7c94e3a05d1
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3f8a24b91e7"
down_revision = "b7c94e3a05d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # The secret sent in the link. Indexed and unique: it is the only thing
        # the confirm endpoint has to go on.
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        # Single use. Kept as a flag rather than deleting the row so a second
        # click on the same link can be told it was already used.
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # Deleting a user takes their outstanding reset links with them --
        # otherwise a token could outlive the account it unlocks.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_password_resets_token", "password_resets", ["token"], unique=True)
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_index("ix_password_resets_token", table_name="password_resets")
    op.drop_table("password_resets")
