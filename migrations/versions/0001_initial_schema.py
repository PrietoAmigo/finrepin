"""Initial schema: instruments, prices, filings, fundamentals, earnings_dates.

Revision ID: 0001
Revises:
Create Date: 2026-07-17

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("yahoo_symbol", sa.String(length=32), nullable=True),
        sa.Column("coingecko_id", sa.String(length=64), nullable=True),
        sa.Column("cik", sa.String(length=10), nullable=True),
        sa.Column("taxonomy", sa.String(length=16), nullable=True),
    )

    op.create_table(
        "prices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(20, 8), nullable=True),
        sa.Column("high", sa.Numeric(20, 8), nullable=True),
        sa.Column("low", sa.Numeric(20, 8), nullable=True),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("instrument_id", "date", name="uq_prices_instrument_date"),
    )
    op.create_index("ix_prices_instrument_id", "prices", ["instrument_id"])
    op.create_index("ix_prices_date", "prices", ["date"])

    op.create_table(
        "filings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("accession_no", sa.String(length=25), nullable=False, unique=True),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_filings_instrument_id", "filings", ["instrument_id"])
    op.create_index("ix_filings_filed_at", "filings", ["filed_at"])

    op.create_table(
        "fundamentals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("taxonomy", sa.String(length=16), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(28, 6), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=4), nullable=True),
        sa.Column("form", sa.String(length=16), nullable=True),
        sa.Column("accession_no", sa.String(length=25), nullable=True),
        sa.Column("filed_at", sa.Date(), nullable=True),
        sa.UniqueConstraint(
            "instrument_id",
            "taxonomy",
            "tag",
            "unit",
            "period_start",
            "period_end",
            name="uq_fundamentals_fact",
        ),
    )
    op.create_index("ix_fundamentals_instrument_id", "fundamentals", ["instrument_id"])
    op.create_index(
        "ix_fundamentals_instrument_tag", "fundamentals", ["instrument_id", "tag"]
    )
    op.create_index("ix_fundamentals_filed_at", "fundamentals", ["filed_at"])

    op.create_table(
        "earnings_dates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("earnings_date", sa.Date(), nullable=False),
        sa.Column("is_estimated", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("earnings_dates")
    op.drop_table("fundamentals")
    op.drop_table("filings")
    op.drop_table("prices")
    op.drop_table("instruments")
