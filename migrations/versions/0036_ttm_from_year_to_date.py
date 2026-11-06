"""TTM figures: quarters from year-to-date facts, so they refresh every quarter.

equity_flows_quarterly (0002, inlined in 0029) took a quarter only from a
reported three-month fact, plus Q4 as the full year less three such quarters.
A 10-Q reports its cash flow statement year to date only (3, 6 and 9
months), so for SEC filers operating cash flow, capex and D&A had Q1 alone:
equity_flows_ttm could never sum four quarters and fell back to the 10-K
figure, and TTM free cash flow, P/FCF and EV/EBITDA moved once a year.

A quarter is now, as in the quarterly statements (0035), a reported
three-month figure or the year to date less the year to date a quarter
earlier, within one tag, unit and start: Q2 = half year - Q1, Q3 = nine
months - half year, Q4 = full year - nine months. The tag is chosen as in
fundamentals_grouped (family priority, then a reported figure over a derived
one, then USD), and 0008's Q4 (the full year less the three reported quarters
inside it) stays as a last resort for a year with no nine-month figure.

What changes:

- ocf, capex and d_a get Q2-Q4, so their TTM has a point at every quarter
  end, not only at fiscal-year ends;
- the income families (revenue, eps, op_income, net_income, gross_profit,
  cogs) keep their reported quarters; their Q4 becomes full year less nine
  months, the same as the Financial Statements quarters. It equals the old
  Q4 unless a filing restated a quarter;
- one row per instrument, family and quarter end (two facts of different
  start ending the same day used to give two rows, and a five-row TTM sum).

equity_flows_ttm is unchanged: it sums the last four quarters and prefers the
filed annual figure at fiscal-year ends. Output columns are the old ones, so
CREATE OR REPLACE keeps the views built on it.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-06

"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAMILY_ROW = re.compile(r"\('(\w+)', '(\w+)', (\d+)\)")
QUARTER_DAYS = "BETWEEN 60 AND 120"
PREFERRED_UNITS = "('USD', 'USD/shares', 'shares')"
# Sorts 0008's full-year-less-three-quarters Q4 after every tag's own quarters.
FALLBACK_PRIORITY = 1000


def _load(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flow_families() -> list[tuple[str, str, int]]:
    """fundamentals_grouped's (family, tag, priority) rows (0008 + 0034), flow families only."""
    rev0008 = _load("0008_valuation_metrics.py", "fintracker_migration_0008")
    rev0034 = _load("0034_legacy_xbrl_tags.py", "fintracker_migration_0034")
    text = str(rev0034.families_0008()) + "," + str(rev0034.LEGACY_FAMILIES)
    grps = set(re.findall(r"'(\w+)'", str(rev0008._NEW_FLOW_GRPS)))
    rows = [(g, t, int(p)) for g, t, p in FAMILY_ROW.findall(text)]
    assert len(rows) == text.count("('"), "a family row didn't parse"
    return [r for r in rows if r[0] in grps]


def flow_groups() -> str:
    rev0008 = _load("0008_valuation_metrics.py", "fintracker_migration_0008")
    return str(rev0008._NEW_FLOW_GRPS)


def flows_quarterly_sql() -> str:
    families = ",\n".join(f"    ('{g}', '{t}', {p})" for g, t, p in flow_families())
    return f"""
CREATE OR REPLACE VIEW equity_flows_quarterly AS
WITH families(grp, tag, priority) AS (VALUES
{families}
),
-- Flow facts from three months to a year, per tag and unit, each next to the
-- fact of the same tag, unit and start that ends just before it: for a
-- year-to-date figure, the year to date a quarter earlier.
facts AS (
    SELECT f.instrument_id, m.grp, m.priority, f.unit, f.period_end,
           (f.period_end - f.period_start) AS period_days,
           f.value::float8 AS value,
           LAG(f.period_end) OVER w AS prev_end,
           LAG(f.value::float8) OVER w AS prev_value
    FROM fundamentals f
    JOIN families m ON m.tag = f.tag
    WHERE (f.period_end - f.period_start) BETWEEN 60 AND 400
    WINDOW w AS (PARTITION BY f.instrument_id, m.grp, m.priority, f.unit, f.period_start
                 ORDER BY f.period_end)
),
-- A reported quarter, or the year to date less the year to date a quarter
-- earlier.
own_quarters AS (
    SELECT instrument_id, grp, priority, unit, period_end,
           CASE WHEN period_days {QUARTER_DAYS} THEN value ELSE value - prev_value END AS value,
           CASE WHEN period_days {QUARTER_DAYS} THEN 0 ELSE 1 END AS derived
    FROM facts
    WHERE period_days {QUARTER_DAYS}
       OR (period_end - prev_end) {QUARTER_DAYS}
),
-- 0008's Q4, for a year with no nine-month figure: the full year less the
-- three reported quarters inside it.
quarterly AS NOT MATERIALIZED (
    SELECT instrument_id, grp, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN {flow_groups()}
      AND period_days {QUARTER_DAYS}
),
annual AS (
    SELECT instrument_id, grp, period_start, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN {flow_groups()}
      AND period_days BETWEEN 330 AND 400
),
derived_q4 AS (
    SELECT a.instrument_id, a.grp, a.period_end,
           a.value - SUM(q.value) AS value
    FROM annual a
    JOIN quarterly q
      ON q.instrument_id = a.instrument_id
     AND q.grp = a.grp
     AND q.period_end > a.period_start
     AND q.period_end < (a.period_end - 45)
    WHERE NOT EXISTS (
        SELECT 1
        FROM quarterly q2
        WHERE q2.instrument_id = a.instrument_id
          AND q2.grp = a.grp
          AND q2.period_end > (a.period_end - 45)
          AND q2.period_end <= (a.period_end + 5)
    )
    GROUP BY a.instrument_id, a.grp, a.period_end, a.value
    HAVING COUNT(*) = 3
)
SELECT DISTINCT ON (instrument_id, grp, period_end)
       instrument_id, grp, period_end, value
FROM (
    SELECT instrument_id, grp, period_end, value, priority, derived, unit
    FROM own_quarters
    UNION ALL
    SELECT instrument_id, grp, period_end, value, {FALLBACK_PRIORITY}, 2, NULL
    FROM derived_q4
) c
ORDER BY instrument_id, grp, period_end, priority, derived,
         CASE WHEN unit IN {PREFERRED_UNITS} THEN 0 ELSE 1 END
"""


def upgrade() -> None:
    op.execute(flows_quarterly_sql())


def downgrade() -> None:
    rev0029 = _load("0029_metric_series_per_instrument.py", "fintracker_migration_0029")
    op.execute(rev0029.flows_quarterly_sql())
