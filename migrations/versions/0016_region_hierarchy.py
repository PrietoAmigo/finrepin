"""Generic Spanish region time-series store for the Grafana housing dashboard.

Replaces the CCAA-only ``housing_prices`` table (0015) with a hierarchy that
holds any indicator at any granularity:

* ``regions`` — nation → CCAA → province → municipality, with ``parent_code``
  links so fine-grained data rolls up.
* ``indicators`` — registry of the series tracked (prices, income, population,
  housing stock, ...).
* ``region_observations`` — the generic (region, indicator, period) → value.

Plus two views the dashboard reads: ``v_region_series`` (observations joined to
region + indicator metadata) and ``v_region_yoy`` (year-on-year % change).

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-21

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


V_REGION_SERIES = """
CREATE VIEW v_region_series AS
SELECT
    o.region_code,
    r.name        AS region_name,
    r.level,
    r.ine_code,
    r.parent_code,
    p.name        AS parent_name,
    r.lat,
    r.lon,
    o.indicator,
    i.name        AS indicator_name,
    i.unit,
    i.category,
    o.source,
    o.period,
    o.value
FROM region_observations o
JOIN regions r    ON r.code = o.region_code
LEFT JOIN regions p ON p.code = r.parent_code
LEFT JOIN indicators i ON i.code = o.indicator
"""

# Year-on-year % change: join each observation to the one exactly a year earlier
# (works for annual/quarterly/monthly series, whose periods align on the month).
V_REGION_YOY = """
CREATE VIEW v_region_yoy AS
SELECT
    o.region_code,
    o.indicator,
    o.period,
    o.value,
    prev.value AS value_year_ago,
    CASE WHEN prev.value IS NULL OR prev.value = 0 THEN NULL
         ELSE (o.value / prev.value - 1) * 100 END AS yoy_pct
FROM region_observations o
LEFT JOIN region_observations prev
    ON  prev.region_code = o.region_code
    AND prev.indicator   = o.indicator
    AND prev.period       = (o.period - INTERVAL '1 year')::date
"""


def upgrade() -> None:
    op.drop_table("housing_prices")

    op.create_table(
        "regions",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("ine_code", sa.String(8), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("parent_code", sa.String(16), nullable=True),
        sa.Column("lat", sa.Numeric(9, 4), nullable=True),
        sa.Column("lon", sa.Numeric(9, 4), nullable=True),
    )
    op.create_index("ix_regions_level", "regions", ["level"])
    op.create_index("ix_regions_parent", "regions", ["parent_code"])

    op.create_table(
        "indicators",
        sa.Column("code", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("frequency", sa.String(2), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("higher_is", sa.String(8), nullable=False, server_default="neutral"),
    )

    op.create_table(
        "region_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("region_code", sa.String(16), nullable=False),
        sa.Column("indicator", sa.String(40), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(16, 4), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "region_code", "indicator", "period", name="uq_region_obs_region_indicator_period"
        ),
    )
    op.create_index(
        "ix_region_obs_indicator_period", "region_observations", ["indicator", "period"]
    )
    op.create_index(
        "ix_region_obs_region_indicator", "region_observations", ["region_code", "indicator"]
    )

    op.execute(V_REGION_SERIES)
    op.execute(V_REGION_YOY)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_region_yoy")
    op.execute("DROP VIEW IF EXISTS v_region_series")
    op.drop_index("ix_region_obs_region_indicator", table_name="region_observations")
    op.drop_index("ix_region_obs_indicator_period", table_name="region_observations")
    op.drop_table("region_observations")
    op.drop_table("indicators")
    op.drop_index("ix_regions_parent", table_name="regions")
    op.drop_index("ix_regions_level", table_name="regions")
    op.drop_table("regions")

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
    op.create_index("ix_housing_indicator_period", "housing_prices", ["indicator", "period"])
    op.create_index("ix_housing_region_indicator", "housing_prices", ["region_code", "indicator"])
