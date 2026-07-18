"""Add MCap and Shares outstanding to equity_metric_series.

Daily market cap = close x latest reported weighted diluted shares — the
same share-count window the P/FCF branch already uses. Shares outstanding
is that share series itself, one point per report date. CREATE OR REPLACE
works both ways because the view's column list is unchanged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-17

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The shares_w / eps_w / fcf_w windows and the existing branches are identical
# to migration 0002; the only change is the trailing 'mcap' UNION ALL branch.
_COMMON = """
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

_NEW_BRANCHES = """
UNION ALL
SELECT p.instrument_id, 'mcap', 'MCap', p.date::timestamptz,
       p.close::float8 * s.value
FROM prices p
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
UNION ALL
SELECT instrument_id, 'shares', 'Shares outstanding',
       period_end::timestamptz, value
FROM shares_w
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW equity_metric_series AS {_COMMON} {_NEW_BRANCHES}")


def downgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW equity_metric_series AS {_COMMON}")
