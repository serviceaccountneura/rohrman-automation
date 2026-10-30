"""add email to invite codes

An invite is now addressed to a specific person, and signup must use that
address -- so a forwarded link cannot be redeemed by someone else.

Revision ID: c8d41e70b592
Revises: a3f9c05b71d2
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8d41e70b592"
down_revision = "a3f9c05b71d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invite_codes",
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_index("ix_invite_codes_email", "invite_codes", ["email"])


def downgrade() -> None:
    op.drop_index("ix_invite_codes_email", table_name="invite_codes")
    op.drop_column("invite_codes", "email")
