"""add manual_fields and vehicle_details to documents

Two JSON-in-text columns, both for the vehicle flow but neither specific to it.

`manual_fields` holds what a person typed in after a document was refused --
the stock number the clerk forgot to write on, a GL account OCR could not read.
The pipeline overlays it on what OCR produced and runs the document again.
Kept as its own column rather than overwriting the OCR-derived fields, so the
difference between "the invoice says this" and "a person asserted this" stays
visible afterwards.

`vehicle_details` is the result: what was read, which template matched, and the
postings that were built. Written on every attempt, success or refusal, because
the refused ones are exactly the ones someone needs to look at, and nothing
else in this schema records why a vehicle entry came out the way it did.

Text rather than JSONB. Neither column is ever queried by content -- they are
read back whole for one document -- and JSONB would buy indexing nobody needs
while making every write pay for parsing.

Revision ID: c1e7b350af92
Revises: d3f8a24b91e7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1e7b350af92"
down_revision = "d3f8a24b91e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("manual_fields", sa.String(length=4000), nullable=False, server_default=""),
    )
    op.add_column(
        "documents",
        sa.Column("vehicle_details", sa.String(length=8000), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("documents", "vehicle_details")
    op.drop_column("documents", "manual_fields")
