"""add vendor_mappings table

Revision ID: 967da6cad2f9
Revises: 43067ed3e89f
Create Date: 2026-08-05 09:50:03.561093

"""
from typing import Sequence, Union
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '967da6cad2f9'
down_revision: Union[str, Sequence[str], None] = '43067ed3e89f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Seed data: (dealer_id, vendor_name, vendor_display_id) ────────────────────
# Honda  = dealer 1707 (Schaumburg Honda)
# Toyota = dealer 1711 (Oakbrook Toyota in Westmont)
# Ford   = dealer 1708 (Schaumburg Ford)
# Kia    = dealer 1710 (Bob Rohrman Schaumburg Kia)
VENDOR_SEED = [
    # ── Honda (1707) ──
    ("1707", "ADVANCE AUTO PARTS",        "1707_160"),
    ("1707", "DGO PREMIUM SERVICES",      "1707_960"),
    ("1707", "ILLINOIS RECOVERY GROUP",   "1707_222"),
    ("1707", "ILLINOIS TIRE RECYCLING",   "1707_191"),
    ("1707", "MCGARD",                    "1707_341"),
    ("1707", "OREILLY'S AUTO PARTS",      "1707_9464"),
    ("1707", "S&S AUTOMOTIVE",            "1707_461"),
    ("1707", "TERRACE SUPPLY CO",         "1707_464"),
    ("1707", "WILDMAN FACILITY",          "2626"),

    # ── Toyota (1711) ──
    ("1711", "AMERICAN WELDING & GAS",    "1711_AWG"),
    ("1711", "BALES LUMBER",              "1711_5389"),
    ("1711", "GRAINGER",                  "1711_5323"),
    ("1711", "ILLINOIS RECOVERY GROUP",   "1711_IRG"),
    ("1711", "ILLINOIS TIRE RECYCLING",   "1711_ITR"),
    ("1711", "MCGARD",                    "1711_6996"),
    ("1711", "NAPA",                      "1711_6028"),
    ("1711", "S&S AUTOMOTIVE",            "1711_5016"),
    ("1711", "SUBURBAN DOOR",             "1711_5201"),
    ("1711", "TSD RENTAL",                "2102"),
    ("1711", "VARISITY VENDING",          "1711_VARSITY"),
    ("1711", "WILDMAN",                   "2623"),

    # ── Ford (1708) ──
    ("1708", "ADVANCE AUTO PARTS",        "1708_981"),
    ("1708", "AER TECHNOLOGIES",          "1708_650"),
    ("1708", "AMERICAN WELDING & GAS",    "1708_741"),
    ("1708", "CINTAS",                    "1708_432"),
    ("1708", "DIGITAL COPIER SUPERCENTER","1708_DCS"),
    ("1708", "ILLINOIS RECOVERY GROUP",   "1708_761"),
    ("1708", "ILLINOIS TIRE RECYCLING",   "1708_382"),
    ("1708", "OREILLY AUTO PARTS",        "1708_540"),
    ("1708", "S&S AUTOMOTIVE",            "1708_333"),
    ("1708", "TERRACE SUPPLY",            "1708_436"),
    ("1708", "US AUTO FORCE",             "1708_416"),
    ("1708", "VARSITY VENDING",           "1708_987"),
    ("1708", "WILDMAN FACILITY",          "2624"),

    # ── Kia (1710) ──
    ("1710", "ADVANCE AUTO PARTS",        "1710_ADVANCE"),
    ("1710", "AMERICAN WELDING & GAS",    "1710_347"),
    ("1710", "AUTOZONE",                  "1710_2211"),
    ("1710", "BROADWAY CARWASH",          "2816"),
    ("1710", "CINTAS",                    "1710_CINTAS"),
    ("1710", "DGO PREMIUM SERVICES",      "1710_126"),
    ("1710", "DIGITAL COPIER SUPERCENTER","1710_1462"),
    ("1710", "GS CLEANING SOLUTIONS",     "2691"),
    ("1710", "ILLINOIS RECOVERY GROUP",   "1710_1119"),
    ("1710", "ILLINOIS TIRE RECYCLING",   "1710_386"),
    ("1710", "OREILLY AUTO PARTS",        "1710_182"),
    ("1710", "S&S AUTOMOTIVE",            "1710_855"),
    ("1710", "WILDMAN FACILITY",          "2625"),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('vendor_mappings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('dealer_id', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('vendor_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('vendor_display_id', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dealer_id', 'vendor_name', name='uq_vendor_mapping_dealer_vendor')
    )
    op.create_index(op.f('ix_vendor_mappings_dealer_id'), 'vendor_mappings', ['dealer_id'], unique=False)
    op.create_index(op.f('ix_vendor_mappings_vendor_name'), 'vendor_mappings', ['vendor_name'], unique=False)

    # Seed vendor mappings
    vendor_mappings_table = sa.table(
        'vendor_mappings',
        sa.column('id', sa.Uuid),
        sa.column('dealer_id', sa.String),
        sa.column('vendor_name', sa.String),
        sa.column('vendor_display_id', sa.String),
        sa.column('created_at', sa.DateTime),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(vendor_mappings_table, [
        {
            "id": str(uuid.uuid4()),
            "dealer_id": dealer_id,
            "vendor_name": vendor_name,
            "vendor_display_id": display_id,
            "created_at": now,
        }
        for dealer_id, vendor_name, display_id in VENDOR_SEED
    ])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_vendor_mappings_vendor_name'), table_name='vendor_mappings')
    op.drop_index(op.f('ix_vendor_mappings_dealer_id'), table_name='vendor_mappings')
    op.drop_table('vendor_mappings')
