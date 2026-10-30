"""add document soft delete

Deleting a document from the queue must not destroy it. The row is marked and
greyed out in the UI instead: the invoice, what OCR read, and anything already
posted to Tekion all stay on record, because a document that reached Tekion is
part of the audit trail whether or not anyone wants to see it in the list.

`deleted_at` doubles as the flag -- NULL means live -- so there is no second
boolean to disagree with it. `deleted_by_id` records who, and is SET NULL on a
user being removed, like `uploaded_by_id`.

Revision ID: b8e5d21ca907
Revises: a2f6c40d81b5
"""
from alembic import op
import sqlalchemy as sa

revision = "b8e5d21ca907"
down_revision = "a2f6c40d81b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("deleted_by_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_documents_deleted_by_id_users",
        "documents",
        "users",
        ["deleted_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Every list and every count filters on this, so it earns an index.
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_deleted_at", table_name="documents")
    op.drop_constraint("fk_documents_deleted_by_id_users", "documents", type_="foreignkey")
    op.drop_column("documents", "deleted_by_id")
    op.drop_column("documents", "deleted_at")
