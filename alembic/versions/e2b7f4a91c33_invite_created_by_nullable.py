"""allow invite_codes.created_by to be null

A user cannot be deleted while any invite still points at them. Nulling the
creator keeps the invite record - who was invited, when, whether it was used -
after the admin who sent it has been removed.

Revision ID: e2b7f4a91c33
Revises: c8d41e70b592
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2b7f4a91c33"
down_revision = "c8d41e70b592"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("invite_codes", "created_by", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Rows whose creator was deleted cannot be restored to NOT NULL; drop them.
    op.execute("DELETE FROM invite_codes WHERE created_by IS NULL")
    op.alter_column("invite_codes", "created_by", existing_type=sa.Uuid(), nullable=False)
