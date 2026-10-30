"""add dealership scoping to users and invites

A user may be restricted to a subset of dealerships. An empty string means
every dealership, which is what existing users and admins get -- so this
migration changes nobody's access.

Revision ID: f5a2c8e13b47
Revises: e2b7f4a91c33
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5a2c8e13b47"
down_revision = "e2b7f4a91c33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("users", "invite_codes"):
        op.add_column(
            table,
            sa.Column("dealerships", sa.String(length=4000), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in ("users", "invite_codes"):
        op.drop_column(table, "dealerships")
