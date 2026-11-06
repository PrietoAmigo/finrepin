"""Quarterly financial statements, next to the annual ones.

The SEC ingest already stores every period a company files, 10-Q quarters
included, and the Yahoo ingest stores Yahoo's quarterly statements, but the
statement views kept only fiscal years. A 10-Q doesn't report every quarter
on its own, though:

- the income statement has the three months and the year to date;
- the cash flow statement has only the year to date (3, 6 and 9 months);
- nobody files Q4: the 10-K carries the full year.

So a quarter is either a reported three-month figure or the difference of two
year-to-date figures that start the same day and end a quarter apart: Q2 is
the first half less Q1, Q3 nine months less the half, Q4 the full year less
nine months. Each difference stays within one tag and one unit, so a quarter
never mixes two tags' definitions or two currencies. The tag a line uses is
then chosen as in the annual view (its mapping priority, then a reported
figure over a derived one, then USD). Two kinds of line are not sums:

- weighted average share counts are averages over time, so a derived quarter
  is time-weighted: (avg_long * days_long - avg_short * days_short) / days
  between the two ends;
- EPS is treated as a sum, as data vendors do, so a derived Q4 EPS (the full
  year less nine months) can differ by a cent from one computed on Q4's own
  share count.

The balance sheet is the instant reported at each quarter end.

Views:

- equity_statement_quarterly: one row per instrument, statement line and
  fiscal quarter, with `fiscal_year` and `fiscal_quarter`. A quarter is
  numbered within the fiscal year that contains it (the annual period ending
  on or after it); quarters after the last filed year count on from that
  year's end. Fiscal years are labeled by the calendar year they end in, as in
  equity_statement_annual.
- equity_statement_periods: the annual and quarterly rows in one view, told
  apart by `freq` ('annual' / 'quarterly') and labeled by `period` ('2025'
  for a fiscal year, '2025 Q3' for a quarter). A filter on `freq` drops the
  other branch at plan time.
- equity_statement_period_matrix: the statement tables' rows, line item x
  period with section headers, like equity_statement_matrix but for either
  frequency, and without the all-blank rows of lines that have no figure in
  any period shown.

Speed: every view is partitioned by instrument_id and every CTE is read once,
so the dashboard's `instrument_id = (SELECT id FROM instruments ...)` filter
reaches the fundamentals index scans and a query computes one company. The
year-to-date differences come from a LAG over facts with the same start, not
a self-join, and the matrix reads the periods view once (each cell yields its
header rows) rather than once for its lines, once for its periods and once
for its cells. Statistics on the period length give the planner real row
counts for the period windows. At about 120 companies and 480k facts, one
company's quarterly table takes some 60 ms, its annual table 5 ms.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-06

"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Duration windows in days (period_end - period_start), as the other views use.
QUARTER_DAYS = "BETWEEN 60 AND 120"
ANNUAL_DAYS = "BETWEEN 330 AND 400"
# Average length of a quarter, for numbering quarters within a fiscal year.
QUARTER_LENGTH = "91.3125"
# Lines whose values are averages over the period, not sums.
AVERAGE_LINES = "'Weighted avg shares%'"
PREFERRED_UNITS = "('USD', 'USD/shares', 'shares')"


def _load(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statement_mapping() -> list[tuple[str, int, str, int, str, int, str]]:
    """The annual view's line mapping: 0007's rows plus 0034's fallbacks."""
    rev0007 = _load("0007_currency_views.py", "fintracker_migration_0007")
    rev0034 = _load("0034_legacy_xbrl_tags.py", "fintracker_migration_0034")
    text = str(rev0007.STATEMENT_MAPPING).rstrip() + "," + str(rev0034.LEGACY_MAPPING)
    # The rows are SQL tuples of quoted names and integers, and none of the
    # names holds a quote, so they read as Python tuples too.
    assert "\'\'" not in text
    return list(ast.literal_eval("[" + text + "]"))


def _values(rows: list[tuple[str, int, str, int, str, int, str]]) -> str:
    return ",\n".join(
        "    (" + ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in row) + ")"
        for row in rows
    )


def quarterly_view_sql() -> str:
    # One VALUES list per statement kind: a filter on a single list would
    # leave Postgres guessing how many lines it keeps.
    rows = statement_mapping()
    flow_rows = [r for r in rows if r[0] in ("income", "cashflow")]
    balance_rows = [r for r in rows if r[0] == "balance"]
    assert len(flow_rows) + len(balance_rows) == len(rows)
    columns = "(statement, section_ord, section, item_ord, line, priority, tag)"
    return f"""
CREATE VIEW equity_statement_quarterly AS
WITH flow_mapping{columns} AS (VALUES
{_values(flow_rows)}
),
balance_mapping{columns} AS (VALUES
{_values(balance_rows)}
),
-- Income and cash-flow facts from three months to a year, per tag and unit,
-- each next to the fact of the same tag, unit and start that ends just
-- before it: for a year-to-date figure, the year to date a quarter earlier.
facts AS (
    SELECT f.instrument_id, m.statement, m.section_ord, m.section,
           (m.section_ord * 100 + m.item_ord) AS ord, m.line, m.priority, f.unit,
           f.period_end, (f.period_end - f.period_start) AS period_days,
           f.value::float8 AS value,
           LAG(f.period_end) OVER w AS prev_end,
           LAG(f.period_end - f.period_start) OVER w AS prev_days,
           LAG(f.value::float8) OVER w AS prev_value
    FROM fundamentals f
    JOIN flow_mapping m ON m.tag = f.tag
    WHERE (f.period_end - f.period_start) BETWEEN 60 AND 400
    WINDOW w AS (PARTITION BY f.instrument_id, m.statement, m.line, m.priority, f.unit,
                              f.period_start
                 ORDER BY f.period_end)
),
-- A reported quarter, or the year to date less the year to date a quarter
-- earlier.
candidates AS (
    SELECT instrument_id, statement, section_ord, section, ord, line, priority, unit,
           period_end,
           CASE WHEN period_days {QUARTER_DAYS} THEN value
                WHEN line LIKE {AVERAGE_LINES}
                THEN (value * (period_days + 1) - prev_value * (prev_days + 1))
                     / (period_end - prev_end)
                ELSE value - prev_value END AS value,
           CASE WHEN period_days {QUARTER_DAYS} THEN 0 ELSE 1 END AS derived
    FROM facts
    WHERE period_days {QUARTER_DAYS}
       OR (period_end - prev_end) {QUARTER_DAYS}
),
flows AS (
    SELECT DISTINCT ON (instrument_id, statement, line, period_end)
           instrument_id, statement, section_ord, section, ord, line, period_end, unit, value
    FROM candidates
    ORDER BY instrument_id, statement, line, period_end, priority, derived,
             CASE WHEN unit IN {PREFERRED_UNITS} THEN 0 ELSE 1 END
),
balance AS (
    SELECT DISTINCT ON (f.instrument_id, m.line, f.period_end)
           f.instrument_id, m.statement, m.section_ord, m.section,
           (m.section_ord * 100 + m.item_ord) AS ord, m.line, f.period_end, f.unit,
           f.value::float8 AS value
    FROM fundamentals f
    JOIN balance_mapping m ON m.tag = f.tag
    WHERE f.period_start = f.period_end
    ORDER BY f.instrument_id, m.line, f.period_end, m.priority,
             CASE WHEN f.unit IN {PREFERRED_UNITS} THEN 0 ELSE 1 END,
             f.filed_at DESC NULLS LAST
),
-- Quarter ends are the period ends with an income or cash-flow figure; the
-- balance sheet is kept only at those.
cells AS (
    SELECT u.*,
           bool_or(u.statement <> 'balance')
               OVER (PARTITION BY u.instrument_id, u.period_end) AS quarter_end
    FROM (
        SELECT instrument_id, statement, section_ord, section, ord, line, period_end, unit,
               value
        FROM flows
        UNION ALL
        SELECT instrument_id, statement, section_ord, section, ord, line, period_end, unit,
               value
        FROM balance
    ) u
),
-- Number each quarter within the fiscal year that contains it (the first
-- annual period ending on or after it), else count on from the last fiscal
-- year before it, else use the calendar quarter.
labeled AS (
    SELECT c.instrument_id, c.statement, c.section_ord, c.section, c.ord, c.line,
           c.period_end, c.unit, c.value,
           CASE WHEN fy.period_end IS NOT NULL
                THEN EXTRACT(YEAR FROM fy.period_end)::int
                WHEN prev.period_end IS NOT NULL
                THEN EXTRACT(YEAR FROM prev.period_end
                             + make_interval(years => (k.n + 3) / 4))::int
                ELSE EXTRACT(YEAR FROM c.period_end)::int END AS fiscal_year,
           CASE WHEN fy.period_end IS NOT NULL
                THEN LEAST(4, GREATEST(1, round(
                         (c.period_end - fy.period_start + 1) / {QUARTER_LENGTH})::int))
                WHEN prev.period_end IS NOT NULL
                THEN (k.n - 1) % 4 + 1
                ELSE EXTRACT(QUARTER FROM c.period_end)::int END AS fiscal_quarter
    FROM cells c
    LEFT JOIN LATERAL (
        SELECT a.period_start, a.period_end
        FROM fundamentals a
        WHERE a.instrument_id = c.instrument_id
          AND (a.period_end - a.period_start) {ANNUAL_DAYS}
          AND a.period_end >= c.period_end
        ORDER BY a.period_end, a.period_start DESC
        LIMIT 1
    ) fy ON fy.period_start < c.period_end
    LEFT JOIN LATERAL (
        SELECT a.period_end
        FROM fundamentals a
        WHERE a.instrument_id = c.instrument_id
          AND (a.period_end - a.period_start) {ANNUAL_DAYS}
          AND a.period_end < c.period_end
        ORDER BY a.period_end DESC
        LIMIT 1
    ) prev ON true
    CROSS JOIN LATERAL (
        SELECT GREATEST(1, round((c.period_end - prev.period_end) / {QUARTER_LENGTH})::int) AS n
    ) k
    WHERE c.quarter_end
)
SELECT DISTINCT ON (instrument_id, statement, line, fiscal_year, fiscal_quarter)
       instrument_id, statement, section_ord, section, ord, line, period_end,
       fiscal_year, fiscal_quarter, value,
       CASE WHEN unit ~ '^[A-Z]{{3}}(/shares)?$' THEN split_part(unit, '/', 1) END AS currency
FROM labeled
ORDER BY instrument_id, statement, line, fiscal_year, fiscal_quarter, period_end DESC
"""


PERIODS_VIEW = """
CREATE VIEW equity_statement_periods AS
SELECT a.instrument_id, 'annual'::text AS freq, a.statement, a.section_ord, a.section,
       a.ord, a.line, a.period_end, a.fiscal_year, NULL::int AS fiscal_quarter,
       a.fiscal_year::text AS period, a.value, a.currency
FROM equity_statement_annual a
UNION ALL
SELECT q.instrument_id, 'quarterly'::text, q.statement, q.section_ord, q.section,
       q.ord, q.line, q.period_end, q.fiscal_year, q.fiscal_quarter,
       q.fiscal_year || ' Q' || q.fiscal_quarter, q.value, q.currency
FROM equity_statement_quarterly q
"""

# Line item x period rows for the statement tables, read from the periods view
# once. Each cell also yields its section's header row and the statement's
# first header row (blue in the dashboard), so the first row of a table spans
# every column and Grafana's grouping-to-matrix lays the columns out in the
# query's order; a line or header with no figure in a column is left blank.
# `period_end` is the column's (the latest end among its cells), for
# ordering, FX conversion and range filters.
PERIOD_MATRIX_VIEW = """
CREATE VIEW equity_statement_period_matrix AS
WITH cells AS (
    SELECT p.instrument_id, p.freq, p.statement, p.section_ord, p.section, p.ord, p.line,
           p.period, p.fiscal_year, p.fiscal_quarter, p.value, p.currency,
           max(p.period_end) OVER (PARTITION BY p.instrument_id, p.freq, p.statement, p.period)
               AS period_end,
           first_value(p.section_ord) OVER statement_sections AS first_section_ord,
           first_value(p.section) OVER statement_sections AS first_section
    FROM equity_statement_periods p
    WINDOW statement_sections AS (PARTITION BY p.instrument_id, p.freq, p.statement
                                  ORDER BY p.section_ord)
)
SELECT DISTINCT c.instrument_id, c.freq, c.statement, r.ord, r.line, c.period,
       c.fiscal_year, c.fiscal_quarter, c.period_end, r.value, r.currency
FROM cells c
CROSS JOIN LATERAL (VALUES
    (c.ord, c.line, c.value, c.currency),
    (c.section_ord * 100, upper(c.section), NULL::float8, NULL::text),
    (c.first_section_ord * 100, upper(c.first_section), NULL::float8, NULL::text)
) AS r(ord, line, value, currency)
"""


# Without statistics on the period length Postgres guesses that a window like
# BETWEEN 60 AND 400 keeps 0.5 % of an instrument's facts (it keeps about
# half), and joins the statement mapping and the facts by nested loops sized
# for a handful of rows.
PERIOD_DAYS_STATISTICS = """
CREATE STATISTICS fundamentals_period_days ON (period_end - period_start) FROM fundamentals
"""


def upgrade() -> None:
    op.execute(PERIOD_DAYS_STATISTICS)
    op.execute("ANALYZE fundamentals")
    op.execute(quarterly_view_sql())
    op.execute(PERIODS_VIEW)
    op.execute(PERIOD_MATRIX_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW equity_statement_period_matrix")
    op.execute("DROP VIEW equity_statement_periods")
    op.execute("DROP VIEW equity_statement_quarterly")
    op.execute("DROP STATISTICS fundamentals_period_days")
