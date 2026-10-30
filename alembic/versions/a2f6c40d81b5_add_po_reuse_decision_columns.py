"""add po reuse decision columns

An invoice that names a purchase order already in Tekion should reuse it rather
than create a second one. Deciding that needs a person, and the pipeline runs in
a worker with nobody to ask, so the document parks in PO_DECISION and waits --
the same shape as the DUPLICATE hold that already exists.

`po_candidate` holds what the lookup found, so the queue can show the PO number,
its vendor and its total without going back to Tekion, and so resuming does not
have to look it up a second time.

`po_choice` is honoured for exactly one run and then cleared, like
`duplicate_override`. A later upload of the same invoice is asked again.

Revision ID: a2f6c40d81b5
Revises: c1e7b350af92
"""
from alembic import op
import sqlalchemy as sa

revision = "a2f6c40d81b5"
down_revision = "c1e7b350af92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("po_candidate", sa.String(length=2000), nullable=False, server_default=""),
    )
    op.add_column(
        "documents",
        sa.Column("po_choice", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("documents", "po_choice")
    op.drop_column("documents", "po_candidate")
