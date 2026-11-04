"""equity_metric_series: count each company's reporting currency once.

0029 looks up each instrument's reporting currency (the most common currency
unit among its facts) in `ccy`, a LATERAL subquery correlated with
instruments.id. The ratio branches join `ccy` to every daily price, and
Postgres estimates the dashboard's time-filtered prices at a few rows (it has
no statistics for `date::timestamptz`), so it puts `ccy` on the inner side of
a nested loop. A correlated subquery runs again on every rescan: the instrument's
~4,000 facts were counted once per daily price. At production scale the P/E,
P/FCF, P/B and EV/EBITDA panels took 7-10 s each, for every company, including
ones that list and report in the same currency.

The reporting currency is now `rep_ccy`, a plain subquery joined on
instrument_id. A dashboard's `instrument_id = (SELECT id ...)` filter reaches
it as a query parameter, which doesn't change between rescans, so Postgres
rewinds the counted result instead of recounting: the four panels take
30-220 ms. Rows are 0029's (most common currency unit; a tie between two
currencies is broken arbitrarily, as before).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-04

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0029's `ccy` with its LATERAL reporting-currency lookup moved into a
# separate, uncorrelated CTE.
CCY = """
WITH rep_ccy AS NOT MATERIALIZED (
    SELECT DISTINCT ON (u.instrument_id) u.instrument_id, u.currency
    FROM (
        SELECT fu.instrument_id, split_part(fu.unit, '/', 1) AS currency, count(*) AS n
        FROM fundamentals fu
        WHERE fu.unit ~ '^[A-Z]{3}(/shares)?$'
        GROUP BY 1, 2
    ) u
    ORDER BY u.instrument_id, u.n DESC
),
ccy AS NOT MATERIALIZED (
    SELECT i.id AS instrument_id,
           i.currency AS price_currency,
           rep.currency AS reporting_currency,
           (SELECT f.id FROM instruments f
            WHERE f.kind = 'forex' AND f.symbol = i.currency || '/USD') AS price_fx,
           (SELECT f.id FROM instruments f
            WHERE f.kind = 'forex' AND f.symbol = rep.currency || '/USD') AS reporting_fx
    FROM instruments i
    LEFT JOIN rep_ccy rep ON rep.instrument_id = i.id
),"""


def _load_0029() -> ModuleType:
    path = Path(__file__).with_name("0029_metric_series_per_instrument.py")
    spec = importlib.util.spec_from_file_location("fintracker_migration_0029", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metric_series_sql() -> str:
    rev0029 = _load_0029()
    sql = str(rev0029.metric_series_sql())
    assert sql.count(rev0029.CCY) == 1, "0029's ccy CTE text changed"
    return sql.replace(rev0029.CCY, CCY)


def upgrade() -> None:
    op.execute(metric_series_sql())


def downgrade() -> None:
    op.execute(_load_0029().metric_series_sql())
