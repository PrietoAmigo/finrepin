"""Metric views backing the per-ticker fundamentals dashboard.

Layered plain (non-materialized) views over `fundamentals` and `prices`:

- fundamentals_grouped     — curated tags collapsed into metric families
                             (revenue, eps, ...), one value per period.
- equity_flows_quarterly   — quarterly income/cash-flow values, with Q4
                             derived as FY minus the three interim quarters.
- equity_flows_ttm         — trailing-twelve-month values per quarter end.
- equity_fcf_ttm           — TTM free cash flow (operating cash flow - capex).
- equity_debt              — total debt composed from the reported components.
- equity_metric_series     — long-format series the dashboard queries:
                             one row per (instrument, metric, time).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Maps raw XBRL tags (us-gaap and ifrs-full) to metric families. Priority
# breaks ties when one filer reports several tags for the same period.
# Keep in sync with CURATED_TAGS in fintracker.ingest.fundamentals.
FUNDAMENTALS_GROUPED = """
CREATE VIEW fundamentals_grouped AS
SELECT DISTINCT ON (f.instrument_id, m.grp, f.period_start, f.period_end)
       f.instrument_id,
       m.grp,
       f.period_start,
       f.period_end,
       (f.period_end - f.period_start) AS period_days,
       f.value::float8 AS value
FROM fundamentals f
JOIN (VALUES
    ('revenue', 'RevenueFromContractWithCustomerExcludingAssessedTax', 1),
    ('revenue', 'Revenues', 2),
    ('revenue', 'Revenue', 3),
    ('eps', 'EarningsPerShareDiluted', 1),
    ('eps', 'DilutedEarningsLossPerShare', 2),
    ('op_income', 'OperatingIncomeLoss', 1),
    ('op_income', 'ProfitLossFromOperatingActivities', 2),
    ('ocf', 'NetCashProvidedByUsedInOperatingActivities', 1),
    ('ocf', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations', 2),
    ('ocf', 'CashFlowsFromUsedInOperatingActivities', 3),
    ('capex', 'PaymentsToAcquirePropertyPlantAndEquipment', 1),
    ('capex', 'PaymentsToAcquireProductiveAssets', 2),
    ('capex', 'PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities', 3),
    ('shares_diluted', 'WeightedAverageNumberOfDilutedSharesOutstanding', 1),
    ('shares_diluted', 'AdjustedWeightedAverageShares', 2),
    ('shares_diluted', 'WeightedAverageShares', 3),
    ('debt_lt_noncurrent', 'LongTermDebtNoncurrent', 1),
    ('debt_lt_noncurrent', 'LongtermBorrowings', 2),
    ('debt_lt_current', 'LongTermDebtCurrent', 1),
    ('debt_lt_current', 'CurrentPortionOfLongtermBorrowings', 2),
    ('debt_lt_total', 'LongTermDebt', 1),
    ('debt_current', 'DebtCurrent', 1),
    ('debt_st', 'ShortTermBorrowings', 1),
    ('debt_st', 'ShorttermBorrowings', 2),
    ('debt_total', 'Borrowings', 1)
) AS m(grp, tag, priority) ON m.tag = f.tag
ORDER BY f.instrument_id, m.grp, f.period_start, f.period_end,
         m.priority,
         CASE WHEN f.unit IN ('USD', 'USD/shares', 'shares') THEN 0 ELSE 1 END,
         f.filed_at DESC NULLS LAST
"""

# Q4 flows are never filed on their own: the 10-K carries the full-year
# figure. Derive Q4 = FY - (Q1 + Q2 + Q3), but only when exactly the three
# interim quarters exist, so annual-only filers don't produce bogus rows.
EQUITY_FLOWS_QUARTERLY = """
CREATE VIEW equity_flows_quarterly AS
WITH quarterly AS (
    SELECT instrument_id, grp, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN ('revenue', 'eps', 'op_income', 'ocf', 'capex')
      AND period_days BETWEEN 60 AND 120
),
annual AS (
    SELECT instrument_id, grp, period_start, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN ('revenue', 'eps', 'op_income', 'ocf', 'capex')
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
SELECT instrument_id, grp, period_end, value FROM quarterly
UNION ALL
SELECT instrument_id, grp, period_end, value FROM derived_q4
"""

# TTM = the last four quarterly rows at each quarter end (row-based, so exact
# fiscal-calendar spacing doesn't matter), provided the oldest of the four is
# recent enough that no quarter is missing in between. Annual filings stand in
# directly at fiscal-year ends (and are preferred there — pref 0 sorts first).
EQUITY_FLOWS_TTM = """
CREATE VIEW equity_flows_ttm AS
WITH windowed AS (
    SELECT instrument_id, grp, period_end,
           SUM(value) OVER w4 AS value,
           LAG(period_end, 3) OVER w AS first_quarter_end
    FROM equity_flows_quarterly
    WINDOW w  AS (PARTITION BY instrument_id, grp ORDER BY period_end),
           w4 AS (PARTITION BY instrument_id, grp ORDER BY period_end
                  ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
),
rolling AS (
    SELECT instrument_id, grp, period_end, value
    FROM windowed
    WHERE first_quarter_end > (period_end - 330)
),
annual AS (
    SELECT instrument_id, grp, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN ('revenue', 'eps', 'op_income', 'ocf', 'capex')
      AND period_days BETWEEN 330 AND 400
)
SELECT DISTINCT ON (instrument_id, grp, period_end)
       instrument_id, grp, period_end, value
FROM (
    SELECT instrument_id, grp, period_end, value, 0 AS pref FROM annual
    UNION ALL
    SELECT instrument_id, grp, period_end, value, 1 AS pref FROM rolling
) u
ORDER BY instrument_id, grp, period_end, pref
"""

# Capex is required (inner join): reporting FCF as bare operating cash flow
# for filers that don't tag capex would overstate it — better no series.
EQUITY_FCF_TTM = """
CREATE VIEW equity_fcf_ttm AS
SELECT o.instrument_id, o.period_end, o.value - c.value AS value
FROM equity_flows_ttm o
JOIN equity_flows_ttm c
  ON c.instrument_id = o.instrument_id
 AND c.period_end = o.period_end
 AND c.grp = 'capex'
WHERE o.grp = 'ocf'
"""

# Total debt from whatever components the filer reports, avoiding double
# counts: DebtCurrent already includes the current portion of long-term debt
# and short-term borrowings; LongTermDebt already includes its current portion.
EQUITY_DEBT = """
CREATE VIEW equity_debt AS
WITH pivot AS (
    SELECT instrument_id, period_end,
           MAX(value) FILTER (WHERE grp = 'debt_lt_noncurrent') AS lt_noncurrent,
           MAX(value) FILTER (WHERE grp = 'debt_lt_total')      AS lt_total,
           MAX(value) FILTER (WHERE grp = 'debt_lt_current')    AS lt_current,
           MAX(value) FILTER (WHERE grp = 'debt_current')       AS debt_current,
           MAX(value) FILTER (WHERE grp = 'debt_st')            AS st_borrowings,
           MAX(value) FILTER (WHERE grp = 'debt_total')         AS borrowings_total
    FROM fundamentals_grouped
    WHERE grp LIKE 'debt%'
    GROUP BY instrument_id, period_end
)
SELECT instrument_id, period_end,
       CASE
           WHEN lt_noncurrent IS NOT NULL THEN
               lt_noncurrent
               + COALESCE(debt_current,
                          COALESCE(lt_current, 0) + COALESCE(st_borrowings, 0))
           WHEN lt_total IS NOT NULL THEN lt_total + COALESCE(st_borrowings, 0)
           WHEN borrowings_total IS NOT NULL THEN borrowings_total
           ELSE COALESCE(debt_current, 0)
                + COALESCE(lt_current, 0)
                + COALESCE(st_borrowings, 0)
       END AS value
FROM pivot
"""

# The long-format series the dashboard reads. Quarterly metrics emit one point
# per quarter end; valuation ratios (P/E, P/FCF) emit one point per trading
# day, joining each price to the fundamentals window it falls into. Ratios go
# null on non-positive denominators, and stale fundamentals (last report older
# than ~15 months) stop producing points rather than freezing the ratio.
EQUITY_METRIC_SERIES = """
CREATE VIEW equity_metric_series AS
WITH eps_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_flows_ttm
    WHERE grp = 'eps'
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
fcf_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_fcf_ttm
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
shares_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM (
        SELECT DISTINCT ON (instrument_id, period_end)
               instrument_id, period_end, value
        FROM fundamentals_grouped
        WHERE grp = 'shares_diluted'
        ORDER BY instrument_id, period_end, period_days ASC
    ) s
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
)
SELECT instrument_id, 'revenue' AS metric, 'Revenue (TTM)' AS label,
       period_end::timestamptz AS "time", value
FROM equity_flows_ttm
WHERE grp = 'revenue'
UNION ALL
SELECT o.instrument_id, 'op_margin', 'Operating margin (TTM)',
       o.period_end::timestamptz, 100.0 * o.value / NULLIF(r.value, 0)
FROM equity_flows_ttm o
JOIN equity_flows_ttm r
  ON r.instrument_id = o.instrument_id
 AND r.period_end = o.period_end
 AND r.grp = 'revenue'
WHERE o.grp = 'op_income'
UNION ALL
SELECT instrument_id, 'debt', 'Total debt', period_end::timestamptz, value
FROM equity_debt
UNION ALL
SELECT p.instrument_id, 'pe', 'P/E (TTM)', p.date::timestamptz,
       CASE WHEN e.value > 0 THEN p.close::float8 / e.value END
FROM prices p
JOIN eps_w e
  ON e.instrument_id = p.instrument_id
 AND p.date >= e.period_end
 AND p.date < e.next_end
 AND p.date < (e.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'pfcf', 'P/FCF (TTM)', p.date::timestamptz,
       CASE WHEN f.value > 0 THEN (p.close::float8 * s.value) / f.value END
FROM prices p
JOIN fcf_w f
  ON f.instrument_id = p.instrument_id
 AND p.date >= f.period_end
 AND p.date < f.next_end
 AND p.date < (f.period_end + 450)
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
"""

# In dependency order; downgrade drops them in reverse.
VIEWS: tuple[tuple[str, str], ...] = (
    ("fundamentals_grouped", FUNDAMENTALS_GROUPED),
    ("equity_flows_quarterly", EQUITY_FLOWS_QUARTERLY),
    ("equity_flows_ttm", EQUITY_FLOWS_TTM),
    ("equity_fcf_ttm", EQUITY_FCF_TTM),
    ("equity_debt", EQUITY_DEBT),
    ("equity_metric_series", EQUITY_METRIC_SERIES),
)


def upgrade() -> None:
    for _name, sql in VIEWS:
        op.execute(sql)


def downgrade() -> None:
    for name, _sql in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
