"""equity_metric_series: compute only the instrument a query asks for.

0028's reporting-currency conversion read `instrument_currencies` for every
daily price. That view's reporting currency is a correlated subquery over the
instrument's fundamentals, so it ran once per price row, and once more for
each place the formula mentions it: tens of millions of rows for one P/E
panel. With a real database the P/E, P/FCF, P/B and EV/EBITDA panels (and the
Watchlist table, which reads P/E and MCap) never finished.

Even before 0028 every panel computed the view for *every* instrument: the
window CTEs (eps_w, shares_w, ...) are referenced by several branches, so
Postgres materialized them in full before filtering, and a filter written as a
join on instruments.symbol can't be pushed into the view at all. Seconds per
panel at a hundred tickers, with eleven panels refreshing at once.

This version:

- marks every window CTE NOT MATERIALIZED, so an `instrument_id = <value>`
  filter reaches each of them (they are all partitioned by instrument_id);
- does the same for `equity_flows_quarterly`, whose `quarterly` CTE (used
  three times) was materialized for every instrument under every TTM figure:
  P/E, EPS, revenue, earnings, margins, P/FCF and EV/EBITDA;
- looks up each instrument's listing currency, reporting currency, and the two
  '<CCY>/USD' FX instruments once per instrument (`ccy`), so the per-price
  work is a single index lookup of the day's FX close, and only for a company
  that lists and reports in different currencies.

The dashboards filter with `instrument_id = (SELECT id FROM instruments ...)`
(or `= ANY(ARRAY(...))`), which Postgres can push down; a join on symbol still
works, just slowly.

Results are 0028's: the same closes, FX source (the latest '<CCY>/USD' close on
or before the day), and fallback to an unconverted price.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-03

"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _load(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fx_close(fx_instrument: str) -> str:
    """The FX instrument's latest close on or before p.date (NULL if none)."""
    return f"""(SELECT r.close::float8 FROM prices r
                    WHERE r.instrument_id = {fx_instrument} AND r.date <= p.date
                    ORDER BY r.date DESC LIMIT 1)"""


def _usd_rate(currency: str, fx_instrument: str) -> str:
    return f"CASE WHEN {currency} = 'USD' THEN 1.0 ELSE {_fx_close(fx_instrument)} END"


# Per instrument, once: listing and reporting currency (as instrument_currencies
# derives it) and the FX instruments that convert each to USD.
CCY = """
WITH ccy AS NOT MATERIALIZED (
    SELECT i.id AS instrument_id,
           i.currency AS price_currency,
           rep.currency AS reporting_currency,
           (SELECT f.id FROM instruments f
            WHERE f.kind = 'forex' AND f.symbol = i.currency || '/USD') AS price_fx,
           (SELECT f.id FROM instruments f
            WHERE f.kind = 'forex' AND f.symbol = rep.currency || '/USD') AS reporting_fx
    FROM instruments i
    LEFT JOIN LATERAL (
        SELECT split_part(fu.unit, '/', 1) AS currency
        FROM fundamentals fu
        WHERE fu.instrument_id = i.id
          AND fu.unit ~ '^[A-Z]{3}(/shares)?$'
        GROUP BY 1
        ORDER BY count(*) DESC
        LIMIT 1
    ) rep ON true
),"""

PX_REP = f"""
px_rep AS NOT MATERIALIZED (
    SELECT p.instrument_id, p.date,
           p.close::float8 * CASE
               WHEN c.reporting_currency IS NULL
                 OR c.reporting_currency = c.price_currency THEN 1
               ELSE COALESCE(
                   ({_usd_rate("c.price_currency", "c.price_fx")})
                   / NULLIF(({_usd_rate("c.reporting_currency", "c.reporting_fx")}), 0),
                   1)
           END AS close
    FROM prices p
    JOIN ccy c ON c.instrument_id = p.instrument_id
),"""

_WINDOW_CTE = re.compile(r"^(WITH )?([a-z_]+_w) AS \($", re.MULTILINE)


def metric_series_sql() -> str:
    rev0012 = _load("0012_price_metric.py", "fintracker_migration_0012")
    rev0028 = _load("0028_ratios_in_reporting_currency.py", "fintracker_migration_0028")
    ctes = str(rev0012._SERIES_CTES)
    names = _WINDOW_CTE.findall(ctes)
    assert [n for _, n in names] == [
        "eps_w", "fcf_w", "shares_w", "equity_w", "cash_w", "debt_w", "ebitda_w"
    ], f"0012 window CTEs changed: {names}"
    # Every window CTE inlined, so an instrument filter reaches it; ccy and
    # px_rep go first.
    ctes = _WINDOW_CTE.sub(lambda m: f"{m.group(2)} AS NOT MATERIALIZED (", ctes)
    ctes = CCY + PX_REP + ctes
    branches = str(rev0012._BASE_BRANCHES + rev0012._NEW_BRANCHES + rev0012._PRICE_BRANCH)
    # 0028's four ratio edits (prices -> px_rep), minus its own CTE.
    for old, new in rev0028._RATIO_EDITS[1:]:
        assert branches.count(old) == 1, f"0012 branch text changed near: {old[:40]!r}"
        branches = branches.replace(old, new)
    return f"{rev0012._VIEW}{ctes}{branches}"


def _flows_quarterly_0008() -> str:
    rev0008 = _load("0008_valuation_metrics.py", "fintracker_migration_0008")
    return str(rev0008._equity_flows_quarterly(rev0008._NEW_FLOW_GRPS))


def flows_quarterly_sql() -> str:
    """0008's equity_flows_quarterly with its CTEs inlined."""
    sql = _flows_quarterly_0008()
    for old, new in (
        ("WITH quarterly AS (", "WITH quarterly AS NOT MATERIALIZED ("),
        ("\nannual AS (", "\nannual AS NOT MATERIALIZED ("),
        ("\nderived_q4 AS (", "\nderived_q4 AS NOT MATERIALIZED ("),
    ):
        assert sql.count(old) == 1, f"0008 flows view text changed near: {old!r}"
        sql = sql.replace(old, new)
    return sql


def upgrade() -> None:
    op.execute(flows_quarterly_sql())
    op.execute(metric_series_sql())


def downgrade() -> None:
    op.execute(_load("0028_ratios_in_reporting_currency.py", "fintracker_migration_0028")
               .ratio_view_sql())
    op.execute(_flows_quarterly_0008())
