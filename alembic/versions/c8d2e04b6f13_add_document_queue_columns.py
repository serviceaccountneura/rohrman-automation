"""add queue columns to documents

The documents table doubles as the job queue: workers claim rows with
SELECT ... FOR UPDATE SKIP LOCKED, so job state and document state stay in one
place and the dashboard sees queue state for free.

  source_path     — where the uploaded file is for the worker to read
  file_hash       — SHA-256 of the bytes, for detecting a re-uploaded file
  attempts        — retry counter
  locked_at /
  locked_by       — held while a worker owns the row
  next_attempt_at — retry backoff; the row is invisible to claims until then
  last_error      — why the last attempt failed

Also switches the default status to QUEUED. Existing PENDING rows are left
alone: they predate the queue and have no file to process.

Revision ID: c8d2e04b6f13
Revises: b3f5a91c7d24
Create Date: 2026-08-18 11:47:02.884511

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c8d2e04b6f13'
down_revision: Union[str, Sequence[str], None] = 'b3f5a91c7d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('source_path', sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('file_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('documents', sa.Column('locked_at', sa.DateTime(), nullable=True))
    op.add_column('documents', sa.Column('locked_by', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('next_attempt_at', sa.DateTime(), nullable=True))
    op.add_column('documents', sa.Column('last_error', sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=False, server_default=''))

    op.create_index(op.f('ix_documents_file_hash'), 'documents', ['file_hash'])
    op.create_index(op.f('ix_documents_next_attempt_at'), 'documents', ['next_attempt_at'])
    # The claim query filters on status and orders by created_at.
    op.create_index('ix_documents_claim', 'documents', ['status', 'next_attempt_at', 'created_at'])

    op.alter_column('documents', 'status', server_default='QUEUED')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('documents', 'status', server_default='PENDING')

    op.drop_index('ix_documents_claim', table_name='documents')
    op.drop_index(op.f('ix_documents_next_attempt_at'), table_name='documents')
    op.drop_index(op.f('ix_documents_file_hash'), table_name='documents')

    op.drop_column('documents', 'last_error')
    op.drop_column('documents', 'next_attempt_at')
    op.drop_column('documents', 'locked_by')
    op.drop_column('documents', 'locked_at')
    op.drop_column('documents', 'attempts')
    op.drop_column('documents', 'file_hash')
    op.drop_column('documents', 'source_path')
