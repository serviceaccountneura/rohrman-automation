"""single token login: drop refresh tokens, add users.tokens_valid_after

A login is now one signed token good for 7 days, with no refresh token. The
refresh_tokens table has nothing left to hold. users.tokens_valid_after takes
over its one remaining job: a password reset refuses every token issued before
it, since there is no server-side session row left to delete.

Revision ID: b3f81d5a6c29
Revises: a9d4c2e7f130
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "b3f81d5a6c29"
down_revision = "a9d4c2e7f130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tokens_valid_after", sa.DateTime(), nullable=True))
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_token_hash"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_jti"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")


def downgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jti", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refresh_tokens_jti"), "refresh_tokens", ["jti"], unique=True)
    op.create_index(op.f("ix_refresh_tokens_token_hash"), "refresh_tokens", ["token_hash"], unique=False)
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)
    op.drop_column("users", "tokens_valid_after")
