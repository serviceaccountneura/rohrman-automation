"""add documents.note + notes_enabled app setting support

Clerk notes on any invoice detail page. The Notes UI is gated by the
``notes_enabled`` runtime flag (Settings toggle); that row is seeded on first
read, so this migration only adds the column.

Revision ID: d8e4b2c1a9f0
Revises: c9f2a1b8e4d0
"""
from alembic import op
import sqlalchemy as sa

revision = "d8e4b2c1a9f0"
down_revision = "c9f2a1b8e4d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("note", sa.String(length=4000), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("documents", "note")
