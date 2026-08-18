"""add je tracking and s3_key to documents

Adds the columns the unified upload pipeline needs:
  s3_key             — where the uploaded file landed, so the row points at it
  ocr_document_type  — what OCR detected, kept next to po_type (which comes from
                       the upload folder and is authoritative) so a mismatch is
                       visible without blocking the run
  transaction_id     — Tekion journal entry id      (OEM folder)
  transaction_number — Tekion journal entry number  (OEM folder)
  journal_id         — "{dealerId}_{journalNumber}" (OEM folder)

Revision ID: b3f5a91c7d24
Revises: 49d305ce3f9b
Create Date: 2026-08-18 10:12:44.318602

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b3f5a91c7d24'
down_revision: Union[str, Sequence[str], None] = '49d305ce3f9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('s3_key', sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('ocr_document_type', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('transaction_id', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('transaction_number', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False, server_default=''))
    op.add_column('documents', sa.Column('journal_id', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'journal_id')
    op.drop_column('documents', 'transaction_number')
    op.drop_column('documents', 'transaction_id')
    op.drop_column('documents', 'ocr_document_type')
    op.drop_column('documents', 's3_key')
