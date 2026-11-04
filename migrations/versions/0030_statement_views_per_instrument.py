"""Financial Statements: compute one ticker's statements, not every ticker's.

At production scale (118 instruments, ~410k facts, 15 FX pairs since 2004)
each Financial Statements table took 40 s to 2 min, and the fiscal-year
picker, which Grafana resolves before any table runs, took another 40 s.
Every query computed the statements of every instrument:

- equity_statement_annual keeps a balance-sheet instant only if the instrument
  has an annual fact ending that day. That EXISTS runs once per balance-sheet
  fact, and with no index on (instrument_id, period_end) each run scanned the
  instrument's whole history: ~170k subplans, ~10M buffer reads per query.
- equity_statement_matrix's `lines` CTE is referenced twice (lines and section
  headers), so Postgres materialized it in full, for every instrument and
  statement, before any filter applied. And the dashboard's filter, a join on
  instruments.symbol, can't be pushed into the views at all.
- The cells were converted through fx_usd_daily joined on the cell's
  currency, which builds every currency's gap-filled calendar (~130k rows of
  window sorts) to read about twenty fiscal-year-end rates.

This migration:

- adds a partial index on fundamentals (instrument_id, period_end) for annual
  facts, so the fiscal-year-end check is one index-only probe. The view text
  is 0007's; its EXISTS already filters on exactly that predicate;
- marks equity_statement_matrix's `lines` CTE NOT MATERIALIZED, so an
  `instrument_id = <value>` filter reaches the fundamentals index scans (the
  other CTEs are referenced once and already inlined);
- adds fx_usd_rate(ccy, day): the latest '<CCY>/USD' close on or before the
  day (1 for USD), as fx_usd_daily forward-fills, NULL before the first quote.
  It is 0028's per-day lookup, two index scans, as a function the dashboard
  calls per cell.

The dashboard filters with `instrument_id = (SELECT id FROM instruments ...)`,
which Postgres pushes down; a join on symbol still works, just slowly. Rows
are 0007's.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-04

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same predicate as equity_statement_annual's fy_ends (0007), so the planner
# matches the partial index to the EXISTS that reads it.
ANNUAL_PREDICATE = "(period_end - period_start) BETWEEN 330 AND 400"

ANNUAL_END_INDEX = f"""
CREATE INDEX ix_fundamentals_annual_end
    ON fundamentals (instrument_id, period_end)
    WHERE {ANNUAL_PREDICATE}
"""

FX_USD_RATE = """
CREATE FUNCTION fx_usd_rate(ccy text, day date) RETURNS float8
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN ccy = 'USD' THEN 1.0::float8 ELSE (
        SELECT r.close::float8
        FROM prices r
        WHERE r.instrument_id = (
                  SELECT f.id FROM instruments f
                  WHERE f.kind = 'forex' AND f.symbol = ccy || '/USD')
          AND r.date <= day
        ORDER BY r.date DESC
        LIMIT 1) END
$$
"""


def _load_0007() -> ModuleType:
    path = Path(__file__).with_name("0007_currency_views.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0007", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _matrix_0007() -> str:
    sql = str(_load_0007()._matrix_view(with_currency=True))
    return sql.replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


def matrix_view_sql() -> str:
    """0007's equity_statement_matrix with `lines` inlined."""
    sql = _matrix_0007()
    old, new = "\nlines AS (\n", "\nlines AS NOT MATERIALIZED (\n"
    assert sql.count(old) == 1, f"0007 matrix view text changed near: {old!r}"
    return sql.replace(old, new)


def upgrade() -> None:
    op.execute(ANNUAL_END_INDEX)
    op.execute(matrix_view_sql())
    op.execute(FX_USD_RATE)


def downgrade() -> None:
    op.execute("DROP FUNCTION fx_usd_rate(text, date)")
    op.execute(_matrix_0007())
    op.execute("DROP INDEX ix_fundamentals_annual_end")
