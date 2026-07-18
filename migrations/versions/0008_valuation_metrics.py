"""New valuation metrics: EV/EBITDA, P/B, EPS, Earnings, Gross margin, D/E.

Extends the metric-family mapping in `fundamentals_grouped` with the extra
families the new ratios need (net income, gross profit, cost of revenue,
D&A, total equity, cash — all built from tags the ingesters already store),
widens the flow views to window the new flow families, and appends the new
branches to `equity_metric_series`:

- eps          — TTM diluted EPS (the series P/E already divides by).
- earnings     — TTM net income.
- gross_margin — TTM gross profit over TTM revenue; falls back to
                 (revenue - cost of revenue) when GrossProfit isn't tagged.
- ev_ebitda    — daily (mcap + total debt - cash) / TTM (op income + D&A);
                 needs D&A tagged, no point drawn while EBITDA <= 0.
- pb           — daily market cap over latest book equity (> 0 only).
- de           — total debt over book equity at each report date.

All views keep their output columns, so CREATE OR REPLACE works both ways.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-18

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Families added on top of the 0002 mapping (kept verbatim below them).
_NEW_FAMILIES = """
    ('net_income', 'NetIncomeLoss', 1),
    ('net_income', 'ProfitLossAttributableToOwnersOfParent', 2),
    ('net_income', 'ProfitLoss', 3),
    ('gross_profit', 'GrossProfit', 1),
    ('cogs', 'CostOfRevenue', 1),
    ('cogs', 'CostOfGoodsAndServicesSold', 2),
    ('cogs', 'CostOfSales', 3),
    ('d_a', 'DepreciationDepletionAndAmortization', 1),
    ('d_a', 'DepreciationAndAmortization', 2),
    ('d_a', 'DepreciationAndAmortisationExpense', 3),
    ('equity', 'StockholdersEquity', 1),
    ('equity', 'EquityAttributableToOwnersOfParent', 2),
    ('equity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', 3),
    ('equity', 'Equity', 4),
    ('cash', 'CashAndCashEquivalentsAtCarryingValue', 1),
    ('cash', 'CashAndCashEquivalents', 2),
"""

_BASE_FAMILIES = """
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
"""


def _fundamentals_grouped(families: str) -> str:
    return f"""
CREATE OR REPLACE VIEW fundamentals_grouped AS
SELECT DISTINCT ON (f.instrument_id, m.grp, f.period_start, f.period_end)
       f.instrument_id,
       m.grp,
       f.period_start,
       f.period_end,
       (f.period_end - f.period_start) AS period_days,
       f.value::float8 AS value
FROM fundamentals f
JOIN (VALUES{families}) AS m(grp, tag, priority) ON m.tag = f.tag
ORDER BY f.instrument_id, m.grp, f.period_start, f.period_end,
         m.priority,
         CASE WHEN f.unit IN ('USD', 'USD/shares', 'shares') THEN 0 ELSE 1 END,
         f.filed_at DESC NULLS LAST
"""


_OLD_FLOW_GRPS = "('revenue', 'eps', 'op_income', 'ocf', 'capex')"
_NEW_FLOW_GRPS = (
    "('revenue', 'eps', 'op_income', 'ocf', 'capex',"
    " 'net_income', 'gross_profit', 'cogs', 'd_a')"
)


def _equity_flows_quarterly(grps: str) -> str:
    return f"""
CREATE OR REPLACE VIEW equity_flows_quarterly AS
WITH quarterly AS (
    SELECT instrument_id, grp, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN {grps}
      AND period_days BETWEEN 60 AND 120
),
annual AS (
    SELECT instrument_id, grp, period_start, period_end, value
    FROM fundamentals_grouped
    WHERE grp IN {grps}
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


def _equity_flows_ttm(grps: str) -> str:
    return f"""
CREATE OR REPLACE VIEW equity_flows_ttm AS
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
    WHERE grp IN {grps}
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


# The 0007 windows plus windows over instant balance-sheet facts (equity,
# cash), EBITDA, and total debt, so daily ratios join each price to the
# fundamentals window it falls into.
_SERIES_CTES = """
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
),
equity_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM (
        SELECT DISTINCT ON (instrument_id, period_end)
               instrument_id, period_end, value
        FROM fundamentals_grouped
        WHERE grp = 'equity' AND period_days = 0
        ORDER BY instrument_id, period_end
    ) e
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
cash_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM (
        SELECT DISTINCT ON (instrument_id, period_end)
               instrument_id, period_end, value
        FROM fundamentals_grouped
        WHERE grp = 'cash' AND period_days = 0
        ORDER BY instrument_id, period_end
    ) c
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
debt_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_debt
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
ebitda_w AS (
    SELECT o.instrument_id, o.period_end,
           COALESCE(LEAD(o.period_end) OVER w, DATE '9999-12-31') AS next_end,
           o.value + d.value AS value
    FROM equity_flows_ttm o
    JOIN equity_flows_ttm d
      ON d.instrument_id = o.instrument_id
     AND d.period_end = o.period_end
     AND d.grp = 'd_a'
    WHERE o.grp = 'op_income'
    WINDOW w AS (PARTITION BY o.instrument_id ORDER BY o.period_end)
)
"""

# The 0007 branches, verbatim.
_BASE_BRANCHES = """
SELECT t.instrument_id, 'revenue' AS metric, 'Revenue (TTM)' AS label,
       t.period_end::timestamptz AS "time", t.value AS value,
       ic.reporting_currency AS currency
FROM equity_flows_ttm t
LEFT JOIN instrument_currencies ic ON ic.instrument_id = t.instrument_id
WHERE t.grp = 'revenue'
UNION ALL
SELECT o.instrument_id, 'op_margin', 'Operating margin (TTM)',
       o.period_end::timestamptz, 100.0 * o.value / NULLIF(r.value, 0),
       NULL::text
FROM equity_flows_ttm o
JOIN equity_flows_ttm r
  ON r.instrument_id = o.instrument_id
 AND r.period_end = o.period_end
 AND r.grp = 'revenue'
WHERE o.grp = 'op_income'
UNION ALL
SELECT d.instrument_id, 'debt', 'Total debt', d.period_end::timestamptz, d.value,
       ic.reporting_currency
FROM equity_debt d
LEFT JOIN instrument_currencies ic ON ic.instrument_id = d.instrument_id
UNION ALL
SELECT p.instrument_id, 'pe', 'P/E (TTM)', p.date::timestamptz,
       CASE WHEN e.value > 0 THEN p.close::float8 / e.value END,
       NULL::text
FROM prices p
JOIN eps_w e
  ON e.instrument_id = p.instrument_id
 AND p.date >= e.period_end
 AND p.date < e.next_end
 AND p.date < (e.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'pfcf', 'P/FCF (TTM)', p.date::timestamptz,
       CASE WHEN f.value > 0 THEN (p.close::float8 * s.value) / f.value END,
       NULL::text
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
UNION ALL
SELECT p.instrument_id, 'mcap', 'MCap', p.date::timestamptz,
       p.close::float8 * s.value,
       ic.price_currency
FROM prices p
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
LEFT JOIN instrument_currencies ic ON ic.instrument_id = p.instrument_id
UNION ALL
SELECT instrument_id, 'shares', 'Shares outstanding',
       period_end::timestamptz, value, NULL::text
FROM shares_w
"""

_NEW_BRANCHES = """
UNION ALL
SELECT t.instrument_id, 'eps', 'EPS (TTM, diluted)', t.period_end::timestamptz,
       t.value, ic.reporting_currency
FROM equity_flows_ttm t
LEFT JOIN instrument_currencies ic ON ic.instrument_id = t.instrument_id
WHERE t.grp = 'eps'
UNION ALL
SELECT t.instrument_id, 'earnings', 'Earnings (TTM)', t.period_end::timestamptz,
       t.value, ic.reporting_currency
FROM equity_flows_ttm t
LEFT JOIN instrument_currencies ic ON ic.instrument_id = t.instrument_id
WHERE t.grp = 'net_income'
UNION ALL
SELECT r.instrument_id, 'gross_margin', 'Gross margin (TTM)',
       r.period_end::timestamptz,
       100.0 * COALESCE(gp.value, r.value - cg.value) / NULLIF(r.value, 0),
       NULL::text
FROM equity_flows_ttm r
LEFT JOIN equity_flows_ttm gp
  ON gp.instrument_id = r.instrument_id
 AND gp.period_end = r.period_end
 AND gp.grp = 'gross_profit'
LEFT JOIN equity_flows_ttm cg
  ON cg.instrument_id = r.instrument_id
 AND cg.period_end = r.period_end
 AND cg.grp = 'cogs'
WHERE r.grp = 'revenue'
  AND (gp.value IS NOT NULL OR cg.value IS NOT NULL)
UNION ALL
SELECT p.instrument_id, 'ev_ebitda', 'EV/EBITDA (TTM)', p.date::timestamptz,
       CASE WHEN eb.value > 0 THEN
           (p.close::float8 * s.value
            + COALESCE(dw.value, 0) - COALESCE(cw.value, 0)) / eb.value
       END,
       NULL::text
FROM prices p
JOIN ebitda_w eb
  ON eb.instrument_id = p.instrument_id
 AND p.date >= eb.period_end
 AND p.date < eb.next_end
 AND p.date < (eb.period_end + 450)
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
LEFT JOIN debt_w dw
  ON dw.instrument_id = p.instrument_id
 AND p.date >= dw.period_end
 AND p.date < dw.next_end
 AND p.date < (dw.period_end + 450)
LEFT JOIN cash_w cw
  ON cw.instrument_id = p.instrument_id
 AND p.date >= cw.period_end
 AND p.date < cw.next_end
 AND p.date < (cw.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'pb', 'P/B', p.date::timestamptz,
       CASE WHEN q.value > 0 THEN (p.close::float8 * s.value) / q.value END,
       NULL::text
FROM prices p
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
JOIN equity_w q
  ON q.instrument_id = p.instrument_id
 AND p.date >= q.period_end
 AND p.date < q.next_end
 AND p.date < (q.period_end + 450)
UNION ALL
SELECT d.instrument_id, 'de', 'Debt-to-Equity', d.period_end::timestamptz,
       CASE WHEN q.value > 0 THEN d.value / q.value END,
       NULL::text
FROM equity_debt d
JOIN (
    SELECT DISTINCT ON (instrument_id, period_end)
           instrument_id, period_end, value
    FROM fundamentals_grouped
    WHERE grp = 'equity' AND period_days = 0
    ORDER BY instrument_id, period_end
) q
  ON q.instrument_id = d.instrument_id
 AND q.period_end = d.period_end
"""


def upgrade() -> None:
    op.execute(_fundamentals_grouped(_BASE_FAMILIES.rstrip() + "," + _NEW_FAMILIES.rstrip(",\n")))
    op.execute(_equity_flows_quarterly(_NEW_FLOW_GRPS))
    op.execute(_equity_flows_ttm(_NEW_FLOW_GRPS))
    op.execute(
        "CREATE OR REPLACE VIEW equity_metric_series AS"
        + _SERIES_CTES
        + _BASE_BRANCHES
        + _NEW_BRANCHES
    )


def downgrade() -> None:
    op.execute(
        "CREATE OR REPLACE VIEW equity_metric_series AS" + _SERIES_CTES + _BASE_BRANCHES
    )
    op.execute(_equity_flows_ttm(_OLD_FLOW_GRPS))
    op.execute(_equity_flows_quarterly(_OLD_FLOW_GRPS))
    op.execute(_fundamentals_grouped(_BASE_FAMILIES))
