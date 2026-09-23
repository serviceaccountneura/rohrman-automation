"""add misc review columns

A Misc invoice now stops before posting so a person can check and correct what
the flow decided. `review_draft` holds that decision -- the fields read and the
GL lines chosen -- and every edit made to it. `review_approved` is set when the
person releases it, and cleared once the posting has been attempted, so an
approval applies to one attempt and is never carried into a later re-run.

Revision ID: d71e3a90bc24
Revises: c4d90b71ea36
"""
from alembic import op
import sqlalchemy as sa

revision = "d71e3a90bc24"
down_revision = "c4d90b71ea36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("review_draft", sa.String(length=8000), nullable=False, server_default=""),
    )
    op.add_column(
        "documents",
        sa.Column("review_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("documents", "review_approved")
    op.drop_column("documents", "review_draft")
