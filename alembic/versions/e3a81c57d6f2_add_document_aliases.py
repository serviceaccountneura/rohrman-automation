"""add document aliases

A re-upload of an invoice whose earlier document failed is processed on that
earlier document instead of becoming a second one. The re-upload's own row is
removed, and this table maps its id to the document it went into, so anything
still polling the id it was given at upload follows it there.

Revision ID: e3a81c57d6f2
Revises: d71e3a90bc24
"""
from alembic import op
import sqlalchemy as sa

revision = "e3a81c57d6f2"
down_revision = "d71e3a90bc24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_aliases",
        sa.Column("alias_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("alias_id"),
    )
    op.create_index(
        "ix_document_aliases_document_id", "document_aliases", ["document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_aliases_document_id", table_name="document_aliases")
    op.drop_table("document_aliases")
