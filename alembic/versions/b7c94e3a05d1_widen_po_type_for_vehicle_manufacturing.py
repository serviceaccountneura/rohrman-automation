"""widen po_type for VEHICLE_MANUFACTURING

The folder key is stored in documents.po_type, which was varchar(20). That fit
every folder there had ever been -- SUBLET, MISCELLANEOUS, STOCK, OEM -- and
then VEHICLE_MANUFACTURING arrived at 21 characters and every upload into the
new folder failed at the INSERT with a 500.

Widened to 40 rather than 21: the next folder name should not need a migration,
and on Postgres a varchar length change is a catalog-only operation regardless
of the size, so there is nothing to be gained by being tight about it.

Revision ID: b7c94e3a05d1
Revises: c3e8a1f4b209
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c94e3a05d1"
down_revision = "c3e8a1f4b209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "documents",
        "po_type",
        existing_type=sa.VARCHAR(length=20),
        type_=sa.VARCHAR(length=40),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Narrowing truncates, so anything already filed under a folder name longer
    # than 20 characters would fail this. Clear those rows' po_type first if a
    # downgrade is ever genuinely needed.
    op.alter_column(
        "documents",
        "po_type",
        existing_type=sa.VARCHAR(length=40),
        type_=sa.VARCHAR(length=20),
        existing_nullable=True,
    )
