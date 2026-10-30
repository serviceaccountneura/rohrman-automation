"""refresh token rotated_at

Records when a refresh token was exchanged for a new pair, so a request that
was already in flight with the same token can still succeed for a few seconds
instead of ending the session. See ROTATION_GRACE in api/routes/auth.py.

Revision ID: a9d4c2e7f130
Revises: e3a81c57d6f2
"""
from alembic import op
import sqlalchemy as sa

revision = "a9d4c2e7f130"
down_revision = "e3a81c57d6f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("rotated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_tokens", "rotated_at")
