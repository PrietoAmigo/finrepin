"""Portfolio benchmark: the same money, put into one fund instead.

- portfolio_benchmark_daily_in(ccy, benchmark) — per account and calendar
  day, the units and value of a shadow position in the `benchmark`
  instrument (an `instruments.symbol`) that received exactly the portfolio's
  cash flows: each buy's outlay (fees included) buys units at that day's
  benchmark close, each sell's proceeds (net of fees) sell units. Both sides
  are in `ccy`, converted at the trade date's FX rate, as in 0023.

Because the cash in and out is identical, portfolio value minus benchmark
value is exactly how far the holdings are ahead of (or behind) the fund. A
sell that takes out more than the fund would have held leaves negative units:
the holdings made more than the fund could have paid out.

The fund's closes are forward-filled across weekends and holidays; a trade
dated before the fund's first close buys at that first close. Pick an
accumulating fund (seed.py registers the iShares Core MSCI World Acc as
MSCIWORLD) so dividends are already in its price.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-09

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PORTFOLIO_BENCHMARK_DAILY_IN = """
CREATE FUNCTION portfolio_benchmark_daily_in(ccy text, benchmark text)
RETURNS TABLE (account_id integer, date date, units float8, value float8)
LANGUAGE sql STABLE
AS $fn$
WITH bars AS (
    SELECT p.date,
           p.close::float8 * COALESCE(src.usd_rate, 1)
               / COALESCE(NULLIF(dst.usd_rate, 0), 1) AS price
    FROM prices p
    JOIN instruments i ON i.id = p.instrument_id
    LEFT JOIN fx_usd_daily src ON src.currency = i.currency AND src.date = p.date
    LEFT JOIN fx_usd_daily dst ON dst.currency = ccy AND dst.date = p.date
    WHERE i.symbol = benchmark AND p.close > 0
),
cal AS (
    SELECT generate_series(
               LEAST((SELECT min(trade_date) FROM portfolio_transactions),
                     (SELECT min(b.date) FROM bars b)),
               CURRENT_DATE, interval '1 day')::date AS date
),
grouped AS (
    SELECT c.date, b.price, count(b.price) OVER (ORDER BY c.date) AS grp
    FROM cal c
    LEFT JOIN bars b ON b.date = c.date
),
daily AS (
    SELECT g.date,
           COALESCE(max(g.price) OVER (PARTITION BY g.grp),
                    (SELECT b.price FROM bars b ORDER BY b.date LIMIT 1)) AS price
    FROM grouped g
),
traded AS (
    SELECT t.account_id, t.trade_date AS date,
           sum(CASE WHEN t.side = 'buy' THEN t.quantity * t.price + t.fees
                    ELSE -(t.quantity * t.price - t.fees)
               END / d.price) AS units
    FROM portfolio_txn_state_in(ccy) t
    JOIN daily d ON d.date = t.trade_date
    GROUP BY t.account_id, t.trade_date
),
span AS (
    SELECT t.account_id,
           generate_series(min(t.date), CURRENT_DATE, interval '1 day')::date AS date
    FROM traded t
    GROUP BY t.account_id
),
held AS (
    SELECT s.account_id, s.date,
           sum(t.units) OVER (PARTITION BY s.account_id ORDER BY s.date) AS units
    FROM span s
    LEFT JOIN traded t ON t.account_id = s.account_id AND t.date = s.date
)
SELECT h.account_id, h.date, h.units, h.units * d.price
FROM held h
JOIN daily d ON d.date = h.date
$fn$
"""


def upgrade() -> None:
    op.execute(PORTFOLIO_BENCHMARK_DAILY_IN)


def downgrade() -> None:
    op.execute("DROP FUNCTION portfolio_benchmark_daily_in(text, text)")
