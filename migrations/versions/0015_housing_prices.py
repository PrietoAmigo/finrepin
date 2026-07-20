"""Spanish housing observations table.

Adds ``housing_prices`` — one row per region × indicator × period — for the
Spain housing dashboard. Region and indicator identities are a static registry
in ``fintracker.housing.regions`` (keyed to the map geometry), so no dimension
tables are needed here.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-20

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "housing_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("region_code", sa.String(length=16), nullable=False),
        sa.Column("indicator", sa.String(length=32), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(14, 4), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.UniqueConstraint(
            "region_code", "indicator", "period", name="uq_housing_region_indicator_period"
        ),
    )
    op.create_index(
        "ix_housing_indicator_period", "housing_prices", ["indicator", "period"]
    )
    op.create_index(
        "ix_housing_region_indicator", "housing_prices", ["region_code", "indicator"]
    )


def downgrade() -> None:
    op.drop_index("ix_housing_region_indicator", table_name="housing_prices")
    op.drop_index("ix_housing_indicator_period", table_name="housing_prices")
    op.drop_table("housing_prices")
