"""Widen documents.ro_number for multi-RO Sublet POs.

A Sublet PO can list many repair orders; we store them comma-separated on the
document so the detail page can show every RO.

Revision ID: e1a7c9d2b4f8
Revises: d8e4b2c1a9f0
"""
from alembic import op
import sqlalchemy as sa

revision = "e1a7c9d2b4f8"
down_revision = "d8e4b2c1a9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "documents",
        "ro_number",
        existing_type=sa.String(length=100),
        type_=sa.String(length=500),
        existing_nullable=False,
        server_default="",
    )


def downgrade() -> None:
    op.alter_column(
        "documents",
        "ro_number",
        existing_type=sa.String(length=500),
        type_=sa.String(length=100),
        existing_nullable=False,
        server_default="",
    )
