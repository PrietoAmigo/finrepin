"""Portfolio holdings layer: accounts, holdings (lots), instrument sector/region.

Adds:

- accounts        — a label to book holdings under (e.g. "IBKR", "Coinbase").
- holdings         — one row per tax lot: quantity + total cost acquired in an
                     account at a date, with running quantity_sold/proceeds so
                     a lot can be partially or fully sold without losing its
                     original cost basis. See fintracker.models.Holding for the
                     full lifecycle description; migration 0023 builds the P/L
                     and portfolio-value SQL views on top of this table.
- instruments.sector / instruments.region — best-effort classification for the
  allocation panels, filled from yfinance at ticker registration; NULL for
  existing instruments and for kinds yfinance doesn't classify (crypto,
  metals, indexes, forex).

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-31

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("sector", sa.String(length=64), nullable=True))
    op.add_column("instruments", sa.Column("region", sa.String(length=64), nullable=True))

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
    )

    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(30, 8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(20, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("acquired_at", sa.Date(), nullable=False),
        sa.Column(
            "quantity_sold", sa.Numeric(30, 8), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("proceeds", sa.Numeric(20, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sold_at", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=256), nullable=True),
    )
    op.create_index("ix_holdings_instrument", "holdings", ["instrument_id"])
    op.create_index("ix_holdings_account", "holdings", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_holdings_account", table_name="holdings")
    op.drop_index("ix_holdings_instrument", table_name="holdings")
    op.drop_table("holdings")
    op.drop_table("accounts")
    op.drop_column("instruments", "region")
    op.drop_column("instruments", "sector")
