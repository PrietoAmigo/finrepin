"""Add a "Price" metric to equity_metric_series (daily close, listing currency).

The compare panel's Left/Right axis selectors gain a "Price" option. The metric
is the instrument's daily close in its listing currency, so the panel's existing
FX conversion turns it into the selected display currency (exactly how MCap is
handled). Only the branch is new: every other branch is 0008's definition,
replayed verbatim so CREATE OR REPLACE swaps the view in place with no data loss.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-19

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- equity_metric_series definition from migration 0008, replayed verbatim ---
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

# --- the one new branch: daily close as "Price", in the listing currency so the
# panel's FX conversion applies (equity-scoped like the rest of the view) ---
_PRICE_BRANCH = """
UNION ALL
SELECT p.instrument_id, 'price', 'Price', p.date::timestamptz,
       p.close::float8,
       ic.price_currency
FROM prices p
JOIN instruments i ON i.id = p.instrument_id AND i.kind = 'equity'
LEFT JOIN instrument_currencies ic ON ic.instrument_id = p.instrument_id
"""

_VIEW = "CREATE OR REPLACE VIEW equity_metric_series AS"


def upgrade() -> None:
    op.execute(_VIEW + _SERIES_CTES + _BASE_BRANCHES + _NEW_BRANCHES + _PRICE_BRANCH)


def downgrade() -> None:
    # Back to 0008's view (drops the price branch).
    op.execute(_VIEW + _SERIES_CTES + _BASE_BRANCHES + _NEW_BRANCHES)
