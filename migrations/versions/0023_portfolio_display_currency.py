"""Portfolio in the display currency at trade-date FX, plus a period IRR.

0022 walked the ledger in USD and left the dashboards to divide by *one*
day's display-currency rate. That is right for a market value (a price is
worth today's FX) but wrong for a cost basis: what a holding cost in EUR was
fixed on the day it was bought, yet `cost_usd / eurusd(today)` re-prices it
every day. On the *Portfolio value vs cost basis* chart the EUR cost line
drifted with EUR/USD between trades, even for a portfolio of euro shares; the
same error leaked into average cost, unrealized P/L, and ROIC.

The fix is to walk the ledger in the currency being reported: each trade is
converted into it on its own trade date, each price on its bar's date. The
display currency is a dashboard variable, so the walk becomes a family of
set-returning functions taking it as a parameter:

- portfolio_txn_state_in(ccy)       — the 0022 average-cost walk, in `ccy`.
- portfolio_position_daily_in(ccy)  — forward-filled onto every calendar day.
- portfolio_positions_in(ccy)       — today's slice plus the allocation labels.

The 0022 views keep their names and columns as thin `…_in('USD')` wrappers,
so ad-hoc SQL against them keeps working.

For the *IRR* stat (which replaces ROIC):

- portfolio_cash_flows_in(ccy, from_date, to_date) — the money-weighted view
  of a window, per account: the holdings' value at the close of the day
  before the window as an opening outflow, every buy (outflow) and sell
  (inflow) inside it at the trade-date rate, and the value at its last day as
  a closing inflow.
- xirr(amounts, dates) — the annualised rate that discounts dated cash flows
  to zero, Excel's XIRR convention (365-day years). Solved by bisection on the
  continuous rate ln(1 + r), bracketed relative to the flows' span so `exp()`
  can't overflow; NULL when the flows never change sign. Mirrored in Python by
  `fintracker.portfolio.xirr`, which the unit tests exercise.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-09

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _to_ccy(src: str, dst: str) -> str:
    """Factor converting an amount through two `fx_usd_daily` rows of one day.

    `usd_rate(from) / usd_rate(to)` is exact on that day; a currency with no FX
    history falls back to 1, as in 0022.
    """
    return f"COALESCE({src}.usd_rate, 1) / COALESCE(NULLIF({dst}.usd_rate, 0), 1)"


# Same walk as 0022 (see its comments): quantities stay numeric so a full sell
# lands on a hard 0, money is float8, an over-sell can't divide by zero.
_ZERO_QTY = "0::numeric"
_AVG_BEFORE = (
    "CASE WHEN w.quantity_after > 0 THEN w.cost_after / w.quantity_after"
    " ELSE 0::float8 END"
)
_REALIZED_STEP = (
    "o.quantity * o.price - o.fees"
    f" - LEAST(o.quantity, GREATEST(w.quantity_after, {_ZERO_QTY})) * " + _AVG_BEFORE
)

PORTFOLIO_TXN_STATE_IN = f"""
CREATE FUNCTION portfolio_txn_state_in(ccy text)
RETURNS TABLE (
    id integer, account_id integer, instrument_id integer, trade_date date,
    side text, rn bigint, quantity numeric, price float8, fees float8,
    quantity_after numeric, cost_after float8, realized_delta float8,
    realized_after float8
)
LANGUAGE sql STABLE
AS $fn$
WITH RECURSIVE ordered AS (
    SELECT t.id, t.account_id, t.instrument_id, t.trade_date, t.side::text AS side,
           t.quantity,
           t.price::float8 * {_to_ccy("src", "dst")} AS price,
           t.fees::float8 * {_to_ccy("src", "dst")} AS fees,
           row_number() OVER (PARTITION BY t.account_id, t.instrument_id
                              ORDER BY t.trade_date, t.id) AS rn
    FROM portfolio_transactions t
    LEFT JOIN fx_usd_daily src
      ON src.currency = t.currency AND src.date = t.trade_date
    LEFT JOIN fx_usd_daily dst
      ON dst.currency = ccy AND dst.date = t.trade_date
),
walk AS (
    SELECT o.id, o.account_id, o.instrument_id, o.trade_date, o.side, o.rn,
           o.quantity, o.price, o.fees,
           CASE WHEN o.side = 'buy' THEN o.quantity ELSE -o.quantity END
               AS quantity_after,
           CASE WHEN o.side = 'buy' THEN o.quantity * o.price + o.fees
                ELSE 0::float8 END AS cost_after,
           CASE WHEN o.side = 'buy' THEN 0::float8
                ELSE o.quantity * o.price - o.fees END AS realized_delta,
           CASE WHEN o.side = 'buy' THEN 0::float8
                ELSE o.quantity * o.price - o.fees END AS realized_after
    FROM ordered o
    WHERE o.rn = 1
    UNION ALL
    SELECT o.id, o.account_id, o.instrument_id, o.trade_date, o.side, o.rn,
           o.quantity, o.price, o.fees,
           w.quantity_after
               + CASE WHEN o.side = 'buy' THEN o.quantity ELSE -o.quantity END,
           CASE WHEN o.side = 'buy'
                THEN w.cost_after + o.quantity * o.price + o.fees
                ELSE GREATEST(w.quantity_after - o.quantity, {_ZERO_QTY})
                     * {_AVG_BEFORE}
           END,
           CASE WHEN o.side = 'buy' THEN 0::float8 ELSE {_REALIZED_STEP} END,
           w.realized_after
               + CASE WHEN o.side = 'buy' THEN 0::float8 ELSE {_REALIZED_STEP} END
    FROM walk w
    JOIN ordered o
      ON o.account_id = w.account_id
     AND o.instrument_id = w.instrument_id
     AND o.rn = w.rn + 1
)
SELECT id, account_id, instrument_id, trade_date, side, rn,
       quantity, price, fees,
       quantity_after, cost_after, realized_delta, realized_after
FROM walk
$fn$
"""

# 0022's forward fill, fed by the walk in `ccy` and with prices converted into
# `ccy` at each bar's date.
PORTFOLIO_POSITION_DAILY_IN = f"""
CREATE FUNCTION portfolio_position_daily_in(ccy text)
RETURNS TABLE (
    account_id integer, instrument_id integer, date date, quantity numeric,
    cost_basis float8, realized float8, price float8, market_value float8,
    unrealized float8
)
LANGUAGE sql STABLE
AS $fn$
WITH eod AS (
    SELECT DISTINCT ON (s.account_id, s.instrument_id, s.trade_date)
           s.account_id, s.instrument_id, s.trade_date AS date,
           s.quantity_after, s.cost_after, s.realized_after
    FROM portfolio_txn_state_in(ccy) s
    ORDER BY s.account_id, s.instrument_id, s.trade_date, s.rn DESC
),
span AS (
    SELECT e.account_id, e.instrument_id,
           generate_series(min(e.date), CURRENT_DATE, interval '1 day')::date AS date
    FROM eod e
    GROUP BY e.account_id, e.instrument_id
),
px AS (
    SELECT p.instrument_id, p.date,
           p.close::float8 * {_to_ccy("src", "dst")} AS price
    FROM prices p
    JOIN instruments i ON i.id = p.instrument_id
    LEFT JOIN fx_usd_daily src ON src.currency = i.currency AND src.date = p.date
    LEFT JOIN fx_usd_daily dst ON dst.currency = ccy AND dst.date = p.date
    WHERE EXISTS (
        SELECT 1 FROM portfolio_transactions t WHERE t.instrument_id = p.instrument_id
    )
),
joined AS (
    SELECT s.account_id, s.instrument_id, s.date,
           e.quantity_after, e.cost_after, e.realized_after, x.price,
           count(e.quantity_after) OVER w AS sgrp,
           count(x.price) OVER w AS pgrp
    FROM span s
    LEFT JOIN eod e
      ON e.account_id = s.account_id
     AND e.instrument_id = s.instrument_id
     AND e.date = s.date
    LEFT JOIN px x ON x.instrument_id = s.instrument_id AND x.date = s.date
    WINDOW w AS (PARTITION BY s.account_id, s.instrument_id ORDER BY s.date)
),
filled AS (
    SELECT j.account_id, j.instrument_id, j.date,
           max(j.quantity_after) OVER (PARTITION BY j.account_id, j.instrument_id, j.sgrp)
               AS quantity,
           max(j.cost_after) OVER (PARTITION BY j.account_id, j.instrument_id, j.sgrp)
               AS cost_basis,
           max(j.realized_after) OVER (PARTITION BY j.account_id, j.instrument_id, j.sgrp)
               AS realized,
           max(j.price) OVER (PARTITION BY j.account_id, j.instrument_id, j.pgrp)
               AS price
    FROM joined j
)
SELECT f.account_id, f.instrument_id, f.date, f.quantity, f.cost_basis, f.realized,
       f.price,
       f.quantity * f.price AS market_value,
       f.quantity * f.price - f.cost_basis AS unrealized
FROM filled f
$fn$
"""

PORTFOLIO_POSITIONS_IN = """
CREATE FUNCTION portfolio_positions_in(ccy text)
RETURNS TABLE (
    account_id integer, account text, instrument_id integer, symbol text,
    instrument text, kind text, currency text, asset_class text, sector text,
    region text, date date, quantity numeric, cost_basis float8, price float8,
    market_value float8, unrealized float8, realized float8, avg_cost float8,
    unrealized_pct float8
)
LANGUAGE sql STABLE
AS $fn$
SELECT d.account_id, a.name::text, d.instrument_id,
       i.symbol::text, i.name::text, i.kind::text, i.currency::text,
       CASE i.kind
            WHEN 'equity' THEN 'Equities'
            WHEN 'crypto' THEN 'Crypto'
            WHEN 'metal'  THEN 'Precious metals'
            WHEN 'index'  THEN 'Funds & indexes'
            WHEN 'forex'  THEN 'Cash & FX'
            ELSE initcap(i.kind)
       END,
       COALESCE(i.sector, 'Unclassified')::text,
       COALESCE(i.region, 'Unclassified')::text,
       d.date, d.quantity, d.cost_basis, d.price, d.market_value,
       d.unrealized, d.realized,
       CASE WHEN d.quantity <> 0 THEN d.cost_basis / d.quantity END,
       CASE WHEN d.cost_basis > 0 THEN 100.0 * d.unrealized / d.cost_basis END
FROM portfolio_position_daily_in(ccy) d
JOIN accounts a ON a.id = d.account_id
JOIN instruments i ON i.id = d.instrument_id
WHERE d.date = CURRENT_DATE
$fn$
"""

# One window's flows, investor's sign convention (cash out < 0). The opening
# value sits on the day before the window so a trade on its first day is a
# flow, not part of the opening mark; a window reaching past today closes on
# today's mark.
PORTFOLIO_CASH_FLOWS_IN = """
CREATE FUNCTION portfolio_cash_flows_in(ccy text, from_date date, to_date date)
RETURNS TABLE (account_id integer, date date, amount float8, kind text)
LANGUAGE sql STABLE
AS $fn$
WITH bounds AS (
    SELECT from_date - 1 AS opening, LEAST(to_date, CURRENT_DATE) AS closing
),
marks AS (
    SELECT d.account_id, d.date, sum(d.market_value) AS value
    FROM portfolio_position_daily_in(ccy) d, bounds b
    WHERE d.date IN (b.opening, b.closing)
    GROUP BY d.account_id, d.date
    HAVING sum(d.market_value) IS NOT NULL
)
SELECT m.account_id, m.date, -m.value, 'opening'
FROM marks m, bounds b
WHERE m.date = b.opening AND b.opening < b.closing
UNION ALL
SELECT t.account_id, t.trade_date,
       CASE WHEN t.side = 'buy' THEN -(t.quantity * t.price + t.fees)
            ELSE t.quantity * t.price - t.fees
       END,
       t.side
FROM portfolio_txn_state_in(ccy) t, bounds b
WHERE t.trade_date > b.opening AND t.trade_date <= b.closing
UNION ALL
SELECT m.account_id, m.date, m.value, 'closing'
FROM marks m, bounds b
WHERE m.date = b.closing
$fn$
"""

# NPV at continuous rate x (r = e^x − 1), years counted from the first flow.
XIRR_NPV = """
CREATE FUNCTION xirr_npv(x float8, amounts float8[], dates date[])
RETURNS float8
LANGUAGE sql IMMUTABLE
AS $fn$
WITH f AS (
    SELECT a, d FROM unnest(amounts, dates) AS u(a, d) WHERE a <> 0
)
SELECT sum(f.a * exp(-x * (f.d - (SELECT min(d) FROM f)) / 365.0)) FROM f
$fn$
"""

XIRR = """
CREATE FUNCTION xirr(amounts float8[], dates date[])
RETURNS float8
LANGUAGE plpgsql IMMUTABLE
AS $fn$
DECLARE
    years float8;
    lo float8;
    hi float8;
    mid float8;
    f_lo float8;
    f_mid float8;
BEGIN
    SELECT (max(d) - min(d)) / 365.0 INTO years
    FROM unnest(amounts, dates) AS u(a, d)
    WHERE a <> 0;
    IF years IS NULL OR years = 0 THEN
        RETURN NULL;
    END IF;
    -- e^(±30) bounds the return over the whole span, far past anything real,
    -- and keeps every exp() in xirr_npv finite.
    lo := -30 / years;
    hi := 30 / years;
    f_lo := xirr_npv(lo, amounts, dates);
    IF f_lo * xirr_npv(hi, amounts, dates) >= 0 THEN
        RETURN NULL;
    END IF;
    FOR i IN 1..100 LOOP
        mid := (lo + hi) / 2;
        f_mid := xirr_npv(mid, amounts, dates);
        EXIT WHEN f_mid = 0;
        IF (f_mid > 0) = (f_lo > 0) THEN
            lo := mid;
            f_lo := f_mid;
        ELSE
            hi := mid;
        END IF;
    END LOOP;
    -- Only a window of days with a several-hundred-fold gain gets here.
    IF mid > 700 THEN
        RETURN NULL;
    END IF;
    RETURN exp(mid) - 1;
END
$fn$
"""

# The 0022 views, same names and columns, now reading the functions in USD.
USD_VIEWS = (
    """
CREATE VIEW portfolio_txn_state AS
SELECT id, account_id, instrument_id, trade_date, side, rn, quantity,
       price AS price_usd, fees AS fees_usd,
       quantity_after, cost_after, realized_delta, realized_after
FROM portfolio_txn_state_in('USD')
""",
    """
CREATE VIEW portfolio_position_daily AS
SELECT account_id, instrument_id, date, quantity, cost_basis, realized,
       price AS price_usd, market_value, unrealized
FROM portfolio_position_daily_in('USD')
""",
    """
CREATE VIEW portfolio_positions AS
SELECT account_id, account, instrument_id, symbol, instrument, kind, currency,
       asset_class, sector, region, date, quantity, cost_basis,
       price AS price_usd, market_value, unrealized, realized, avg_cost,
       unrealized_pct
FROM portfolio_positions_in('USD')
""",
)


def _drop_views() -> None:
    op.execute("DROP VIEW portfolio_positions")
    op.execute("DROP VIEW portfolio_position_daily")
    op.execute("DROP VIEW portfolio_txn_state")


def _revision_0022() -> ModuleType:
    """Load 0022, whose view SQL the downgrade restores verbatim."""
    path = Path(__file__).with_name("0022_portfolio.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0022", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    _drop_views()
    op.execute(PORTFOLIO_TXN_STATE_IN)
    op.execute(PORTFOLIO_POSITION_DAILY_IN)
    op.execute(PORTFOLIO_POSITIONS_IN)
    op.execute(PORTFOLIO_CASH_FLOWS_IN)
    op.execute(XIRR_NPV)
    op.execute(XIRR)
    for view in USD_VIEWS:
        op.execute(view)


def downgrade() -> None:
    _drop_views()
    op.execute("DROP FUNCTION xirr(float8[], date[])")
    op.execute("DROP FUNCTION xirr_npv(float8, float8[], date[])")
    op.execute("DROP FUNCTION portfolio_cash_flows_in(text, date, date)")
    op.execute("DROP FUNCTION portfolio_positions_in(text)")
    op.execute("DROP FUNCTION portfolio_position_daily_in(text)")
    op.execute("DROP FUNCTION portfolio_txn_state_in(text)")
    rev0022 = _revision_0022()
    op.execute(rev0022.PORTFOLIO_TXN_STATE)
    op.execute(rev0022.PORTFOLIO_POSITION_DAILY)
    op.execute(rev0022.PORTFOLIO_POSITIONS)
