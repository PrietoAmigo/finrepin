"""Portfolio benchmark: optionally start from the holdings' value on a given day.

0024's shadow fund position follows the ledger's cash flows from the very
first trade, so on a chart of the last year it rarely starts where the
portfolio does. `portfolio_benchmark_daily_in` gains a third argument:

- start_date (default NULL) — when given, each account's holdings at that
  day's close (market value, in `ccy`) buy fund units at that day's fund
  close, and only trades *after* that day move cash in or out. The benchmark
  then equals the portfolio's value on `start_date` and diverges from there.
  NULL keeps 0024's behaviour: every trade since the first is a cash flow.

The Portfolio dashboard passes the first day of the selected time range.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-09

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PORTFOLIO_BENCHMARK_DAILY_IN = """
CREATE FUNCTION portfolio_benchmark_daily_in(
    ccy text, benchmark text, start_date date DEFAULT NULL
)
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
opening AS (
    -- The holdings at start_date's close switch into the fund at its close.
    SELECT p.account_id, p.date, sum(p.market_value) / d.price AS units
    FROM portfolio_position_daily_in(ccy) p
    JOIN daily d ON d.date = p.date
    WHERE p.date = start_date
    GROUP BY p.account_id, p.date, d.price
),
traded AS (
    SELECT t.account_id, t.trade_date AS date,
           sum(CASE WHEN t.side = 'buy' THEN t.quantity * t.price + t.fees
                    ELSE -(t.quantity * t.price - t.fees)
               END / d.price) AS units
    FROM portfolio_txn_state_in(ccy) t
    JOIN daily d ON d.date = t.trade_date
    WHERE start_date IS NULL OR t.trade_date > start_date
    GROUP BY t.account_id, t.trade_date
),
moves AS (
    SELECT m.account_id, m.date, sum(m.units) AS units
    FROM (SELECT * FROM opening UNION ALL SELECT * FROM traded) m
    GROUP BY m.account_id, m.date
),
span AS (
    SELECT m.account_id,
           generate_series(min(m.date), CURRENT_DATE, interval '1 day')::date AS date
    FROM moves m
    GROUP BY m.account_id
),
held AS (
    SELECT s.account_id, s.date,
           sum(m.units) OVER (PARTITION BY s.account_id ORDER BY s.date) AS units
    FROM span s
    LEFT JOIN moves m ON m.account_id = s.account_id AND m.date = s.date
)
SELECT h.account_id, h.date, h.units, h.units * d.price
FROM held h
JOIN daily d ON d.date = h.date
$fn$
"""


def upgrade() -> None:
    op.execute("DROP FUNCTION portfolio_benchmark_daily_in(text, text)")
    op.execute(PORTFOLIO_BENCHMARK_DAILY_IN)


def downgrade() -> None:
    op.execute("DROP FUNCTION portfolio_benchmark_daily_in(text, text, date)")
    # Restore 0024's two-argument function verbatim.
    path = Path(__file__).with_name("0024_portfolio_benchmark.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0024", path)
    assert spec is not None and spec.loader is not None
    rev0024 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev0024)
    op.execute(rev0024.PORTFOLIO_BENCHMARK_DAILY_IN)
