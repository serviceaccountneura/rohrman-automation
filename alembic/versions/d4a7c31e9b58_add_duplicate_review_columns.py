"""add duplicate review columns to documents

A duplicate is now a decision rather than a failure. When OCR matches an
invoice that was already processed, the run is held at status DUPLICATE and
nobody is billed twice; a user then confirms (reprocess) or discards it.

  duplicate_of       — the already-processed document this one repeats
  duplicate_override — skip the duplicate check for exactly one run, set when
                       a user confirms and cleared as soon as it is honoured

Revision ID: d4a7c31e9b58
Revises: c8d2e04b6f13
Create Date: 2026-08-19 09:31:08.442190

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a7c31e9b58'
down_revision: Union[str, Sequence[str], None] = 'c8d2e04b6f13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'documents',
        sa.Column('duplicate_of', sa.Uuid(), nullable=True),
    )
    op.add_column(
        'documents',
        sa.Column(
            'duplicate_override',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        'fk_documents_duplicate_of',
        'documents',
        'documents',
        ['duplicate_of'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_documents_duplicate_of', 'documents', type_='foreignkey')
    op.drop_column('documents', 'duplicate_override')
    op.drop_column('documents', 'duplicate_of')
