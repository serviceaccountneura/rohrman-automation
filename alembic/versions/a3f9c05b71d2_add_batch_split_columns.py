"""add batch split columns

Several invoices scanned into one file are broken into one document per
invoice. `split_from` points a child at the batch it came from, and
`page_range` records which pages of that batch it is.

Revision ID: a3f9c05b71d2
Revises: d4a7c31e9b58
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f9c05b71d2"
down_revision = "d4a7c31e9b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("split_from", sa.Uuid(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("page_range", sa.String(length=20), nullable=False, server_default=""),
    )
    op.create_foreign_key(
        "fk_documents_split_from", "documents", "documents", ["split_from"], ["id"]
    )
    # Children are always looked up by their parent, never the other way round.
    op.create_index("ix_documents_split_from", "documents", ["split_from"])


def downgrade() -> None:
    op.drop_index("ix_documents_split_from", table_name="documents")
    op.drop_constraint("fk_documents_split_from", "documents", type_="foreignkey")
    op.drop_column("documents", "page_range")
    op.drop_column("documents", "split_from")
