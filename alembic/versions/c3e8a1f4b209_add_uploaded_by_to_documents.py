"""add uploaded_by to documents

Records which user uploaded each document, for the detail page's
"Uploaded by" line. Nullable: rows created before this migration have no
recorded uploader, and if that user's account is later deleted the
reference is nulled rather than blocking the delete.

Revision ID: c3e8a1f4b209
Revises: a91d4f68c205
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e8a1f4b209"
down_revision = "a91d4f68c205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("uploaded_by_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        op.f("ix_documents_uploaded_by_id"),
        "documents",
        ["uploaded_by_id"],
    )
    op.create_foreign_key(
        "documents_uploaded_by_id_fkey",
        "documents",
        "users",
        ["uploaded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "documents_uploaded_by_id_fkey", "documents", type_="foreignkey"
    )
    op.drop_index(op.f("ix_documents_uploaded_by_id"), table_name="documents")
    op.drop_column("documents", "uploaded_by_id")
