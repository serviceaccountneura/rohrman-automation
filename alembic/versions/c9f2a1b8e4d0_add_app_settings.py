"""add app_settings for runtime TEKION_PO_WRITES / OEM_POST toggles

Admins flip these from Settings without editing .env or redeploying. Rows are
seeded from env on first read, so this migration only creates the empty table.

Revision ID: c9f2a1b8e4d0
Revises: b3f81d5a6c29
"""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a1b8e4d0"
down_revision = "b3f81d5a6c29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
