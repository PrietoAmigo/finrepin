"""On-chain metric instruments: a Coin Metrics metric id per series.

Adds `instruments.coinmetrics_metric` so a `kind='onchain'` instrument can name
the free, key-less Coin Metrics Community metric it ingests from (e.g.
``CapMrktCurUSD`` for market cap, ``CapRealUSD`` for realized cap). On-chain
observations reuse the `prices` table (the daily value lands in `close`, like a
forex/crypto spot or an interest-rate row). These feed the Market Overview's
BTC MVRV Z-Score panel, which derives the score in SQL from the two series.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-19

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("coinmetrics_metric", sa.String(length=48), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "coinmetrics_metric")
