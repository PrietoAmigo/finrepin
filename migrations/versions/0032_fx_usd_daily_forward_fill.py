"""fx_usd_daily: forward-fill each close directly instead of joining a calendar.

0007 built the gap-filled calendar by generating every day per currency,
left-joining the closes onto it, numbering the gaps with a running count, and
filling them with a second window over that count: two window passes and
several sorts over every currency's ~8,000 days, with the FX prices
materialized in between. Each Portfolio panel builds it twice, inside its
functions, and that was most of the 1-3 s they take.

This version reads each currency's closes once, finds the next close's date
with LEAD, and expands every close over the days up to that next one (the last
close up to today, as before). Same rows: one per currency per day from its
first close to GREATEST(last close, today), carrying the latest close on or
before the day, plus USD at 1.0. It is ~2.5x cheaper to build in full (0.42 s
-> 0.18 s at 15 currencies since 2004), roughly halving the Portfolio panels,
and a `currency = <value>` filter now reaches the prices scan instead of
stopping at the materialized closes.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-04

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FX_USD_DAILY = """
CREATE OR REPLACE VIEW fx_usd_daily AS
WITH pairs AS (
    SELECT id AS instrument_id, split_part(symbol, '/', 1) AS currency
    FROM instruments
    WHERE kind = 'forex' AND symbol LIKE '%/USD'
),
closes AS (
    SELECT pa.currency, pr.date, pr.close::float8 AS usd_rate,
           LEAD(pr.date) OVER (PARTITION BY pa.currency ORDER BY pr.date) AS next_date
    FROM pairs pa
    JOIN prices pr ON pr.instrument_id = pa.instrument_id
)
SELECT c.currency, d::date AS date, c.usd_rate
FROM closes c
CROSS JOIN LATERAL generate_series(
    c.date,
    COALESCE(c.next_date - 1, GREATEST(c.date, CURRENT_DATE)),
    interval '1 day') AS d
UNION ALL
SELECT 'USD', gs::date, 1.0
FROM generate_series((SELECT min(date) FROM prices),
                     GREATEST((SELECT max(date) FROM prices), CURRENT_DATE),
                     interval '1 day') AS gs
"""


def fx_usd_daily_0007() -> str:
    """0007's definition (0013 recreated it verbatim), for the downgrade."""
    path = Path(__file__).with_name("0007_currency_views.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0007", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.FX_USD_DAILY).replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


def upgrade() -> None:
    op.execute(FX_USD_DAILY)


def downgrade() -> None:
    op.execute(fx_usd_daily_0007())
