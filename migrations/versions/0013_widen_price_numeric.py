"""Widen prices OHLC numerics so trillion-dollar values (e.g. BTC market cap) fit.

The `prices` value columns were ``NUMERIC(20, 8)`` — 12 integer digits, so an
absolute value must stay under 10^12 ($1T). That is ample for share prices, FX,
and rates, but Bitcoin's market cap (stored as a ``kind='onchain'`` close for
the MVRV Z-Score) crossed $1T back in 2021, so the ingest overflowed the column
(``numeric field overflow``). Widen the four value columns to ``NUMERIC(30, 8)``
(22 integer digits) so market/realized cap fit with plenty of headroom.

Two views (``fx_usd_daily`` and ``equity_metric_series``) read ``prices.close``,
and Postgres refuses to change a column type a view depends on. Rather than copy
their (large) definitions here, capture them server-side, drop them, alter the
columns, then recreate them verbatim — all inside one ``DO`` block, which is
static SQL so the offline ``alembic upgrade --sql`` check still works. Nothing
else depends on those two views (the dashboards' FX conversion is done in panel
SQL, not in views), so the drop does not cascade.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-19

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _retype_prices(target: str) -> str:
    """Retype the prices value columns to `target`, preserving the two views
    that depend on `close` by capturing and recreating their definitions."""
    return f"""
DO $$
DECLARE
    fx  text;
    ems text;
BEGIN
    SELECT pg_get_viewdef('fx_usd_daily'::regclass, true)         INTO fx;
    SELECT pg_get_viewdef('equity_metric_series'::regclass, true) INTO ems;
    DROP VIEW equity_metric_series;
    DROP VIEW fx_usd_daily;
    ALTER TABLE prices ALTER COLUMN open  TYPE {target};
    ALTER TABLE prices ALTER COLUMN high  TYPE {target};
    ALTER TABLE prices ALTER COLUMN low   TYPE {target};
    ALTER TABLE prices ALTER COLUMN close TYPE {target};
    EXECUTE 'CREATE VIEW fx_usd_daily AS ' || fx;
    EXECUTE 'CREATE VIEW equity_metric_series AS ' || ems;
END $$;
"""


def upgrade() -> None:
    op.execute(_retype_prices("numeric(30, 8)"))


def downgrade() -> None:
    # Narrowing back can fail if any stored value needs more than 12 integer
    # digits (e.g. an ingested BTC market cap); that is expected for a downgrade.
    op.execute(_retype_prices("numeric(20, 8)"))
