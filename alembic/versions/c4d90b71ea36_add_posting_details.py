"""add posting details

What each document actually posted to Tekion -- the GL accounts, the amounts
against them, and what they add up to. The vehicle flow already recorded this
in `vehicle_details`; every other flow posted its lines and kept no account of
them, so the only way to see where a Misc invoice's money went was to open
Tekion.

Separate from `vehicle_details` rather than folded into it: that column also
holds stock numbers, templates and handwritten annotations, which mean nothing
outside the vehicle flow. This one holds the same shape for all five.

Revision ID: c4d90b71ea36
Revises: b8e5d21ca907
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d90b71ea36"
down_revision = "b8e5d21ca907"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("posting_details", sa.String(length=8000), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("documents", "posting_details")
