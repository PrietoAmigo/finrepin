"""Price ratios in the reporting currency: convert the close before dividing.

P/E, P/FCF, P/B and EV/EBITDA divided the daily close (listing currency) by
per-share or company figures from the statements (reporting currency). For a
company that lists in one currency and reports in another, e.g. CSU.TO (CAD
listing, USD statements), every one of them was off by that exchange rate.

`equity_metric_series` gains one inlined CTE, `px_rep`: each daily close
converted into the instrument's reporting currency at that day's rate, using
the same `<CCY>/USD` closes `fx_usd_daily` is built from (the latest on or
before the day). Where the two currencies match, or a rate is missing, the
factor is 1 (the existing fallback). The four ratio branches read it instead
of `prices`. `Price` and `MCap` stay in the listing currency so the panels'
display-currency conversion still applies.

The CTE is `NOT MATERIALIZED` so the planner inlines it into each branch and
pushes a single-instrument filter down into it, as it does for `prices`.

Every other part of the view is 0012's definition, imported from that
migration and edited with exact, asserted replacements.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-03

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _usd_rate(currency: str) -> str:
    """USD per unit of `currency` on p.date: the latest '<CCY>/USD' close on or
    before it, as fx_usd_daily forward-fills; NULL before the first quote."""
    return f"""CASE WHEN {currency} = 'USD' THEN 1.0 ELSE (
                   SELECT r.close::float8
                   FROM prices r
                   WHERE r.instrument_id = (
                             SELECT f.id FROM instruments f
                             WHERE f.kind = 'forex' AND f.symbol = {currency} || '/USD')
                     AND r.date <= p.date
                   ORDER BY r.date DESC
                   LIMIT 1) END"""


# The CASE short-circuits, so a company that lists and reports in the same
# currency (most of them) never touches FX. The lookups are index scans on
# instruments.symbol and prices (instrument_id, date), rather than joins to
# fx_usd_daily, whose calendar would be rebuilt for every branch.
PX_REP = f"""
WITH px_rep AS NOT MATERIALIZED (
    SELECT p.instrument_id, p.date,
           p.close::float8 * CASE
               WHEN ic.reporting_currency IS NULL
                 OR ic.reporting_currency = ic.price_currency THEN 1
               ELSE COALESCE(
                   ({_usd_rate("ic.price_currency")})
                   / NULLIF(({_usd_rate("ic.reporting_currency")}), 0),
                   1)
           END AS close
    FROM prices p
    JOIN instrument_currencies ic ON ic.instrument_id = p.instrument_id
),
eps_w AS ("""

# (old, new) pairs applied to 0012's text; each old string must occur once.
_RATIO_EDITS = (
    ("\nWITH eps_w AS (", PX_REP),
    (
        "CASE WHEN e.value > 0 THEN p.close::float8 / e.value END,\n"
        "       NULL::text\n"
        "FROM prices p\n",
        "CASE WHEN e.value > 0 THEN p.close / e.value END,\n"
        "       NULL::text\n"
        "FROM px_rep p\n",
    ),
    (
        "CASE WHEN f.value > 0 THEN (p.close::float8 * s.value) / f.value END,\n"
        "       NULL::text\n"
        "FROM prices p\n",
        "CASE WHEN f.value > 0 THEN (p.close * s.value) / f.value END,\n"
        "       NULL::text\n"
        "FROM px_rep p\n",
    ),
    (
        "           (p.close::float8 * s.value\n"
        "            + COALESCE(dw.value, 0) - COALESCE(cw.value, 0)) / eb.value\n"
        "       END,\n"
        "       NULL::text\n"
        "FROM prices p\n",
        "           (p.close * s.value\n"
        "            + COALESCE(dw.value, 0) - COALESCE(cw.value, 0)) / eb.value\n"
        "       END,\n"
        "       NULL::text\n"
        "FROM px_rep p\n",
    ),
    (
        "CASE WHEN q.value > 0 THEN (p.close::float8 * s.value) / q.value END,\n"
        "       NULL::text\n"
        "FROM prices p\n",
        "CASE WHEN q.value > 0 THEN (p.close * s.value) / q.value END,\n"
        "       NULL::text\n"
        "FROM px_rep p\n",
    ),
)


def _rev0012() -> ModuleType:
    path = Path(__file__).with_name("0012_price_metric.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0012", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _view_0012(m: ModuleType) -> str:
    return str(m._VIEW + m._SERIES_CTES + m._BASE_BRANCHES + m._NEW_BRANCHES + m._PRICE_BRANCH)


def ratio_view_sql() -> str:
    """0012's view with the four ratios priced in the reporting currency."""
    sql = _view_0012(_rev0012())
    for old, new in _RATIO_EDITS:
        assert sql.count(old) == 1, f"0012 view text changed near: {old[:40]!r}"
        sql = sql.replace(old, new)
    return sql


def upgrade() -> None:
    op.execute(ratio_view_sql())


def downgrade() -> None:
    op.execute(_view_0012(_rev0012()))
