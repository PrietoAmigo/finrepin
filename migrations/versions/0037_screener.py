"""Screener: every stock's valuation, growth, margins, leverage and moves at once.

The Screener dashboard lists every equity in one table. Its ratios must be the
ones the Valuation dashboard shows for each company, but Valuation's views
compute one company at a time: `equity_flows_ttm` and the views under it
rebuild `fundamentals_grouped` four times, so all equities' TTM figures take
about two seconds, before any ratio, on every refresh and every filter change.
Those figures only change when facts do, so this migration splits the work:

- screener_fundamentals (materialized) — per company, the series built from
  the statements: TTM revenue, diluted EPS, net income, free cash flow and
  EBITDA; gross, operating and net margin, and ROE; diluted shares, book
  equity, cash and total debt; debt-to-equity and debt / EBITDA. Each is a
  point per period end, read from the views Valuation reads
  (equity_flows_ttm, fundamentals_grouped, equity_debt), each computed once.
  Money series carry the reporting currency (as equity_metric_series's
  rep_ccy has it, migration 0031); ratios and share counts carry none. The
  app refreshes it (fintracker.screener) after every fundamentals ingest, on
  an on-demand ticker, and at boot, so a later view change applies too.
- equity_screener — one row per equity. The latest close and the price moves
  are read live from prices, so they are never stale; every fundamentals
  figure is the series' latest point on or before that close, provided it
  ended within 450 days of it: the equity_metric_series windows, so P/E,
  P/FCF, P/B and EV/EBITDA are that view's values on the latest close,
  priced in the reporting currency the same way (0028). Margins, growth and
  leverage take the latest period that reports their inputs, blank when a
  denominator isn't positive.

Growth compares a TTM figure with the one closest to a year earlier (335-395
days), at the latest period that has one, as Valuation's growth chart pairs
them (a Yahoo filer's newest quarterly TTM often has no TTM a year before it,
so its growth is the latest fiscal year's). It stays in the reporting
currency, so an exchange-rate move isn't counted as growth. Price moves
follow the Watchlist and the weekly email: the latest close against the last
close at least that long ago.

On a database the size of production's (81 equities, 380k facts, 1.1M
prices) the refresh takes about 2 s and a read of the whole view about 35 ms.
Every column was checked there, for every company, against the Valuation and
Watchlist dashboards' own queries: P/E, P/FCF, P/B, EV/EBITDA and market cap
equal equity_metric_series on the latest close; margins, growth, debt-to-
equity and debt / EBITDA equal those dashboards' latest points.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# equity_metric_series stops drawing a ratio once its fundamentals are older
# than this many days (migration 0002); the Screener blanks them the same way.
STALE_DAYS = 450

# Series that are amounts of money, in the reporting currency.
MONEY_SERIES = ("revenue", "eps", "net_income", "fcf", "ebitda", "equity", "cash", "debt")

SCREENER_FUNDAMENTALS = """
CREATE MATERIALIZED VIEW screener_fundamentals AS
WITH rep AS (
    -- The reporting currency: the most common currency unit among the
    -- company's facts (equity_metric_series's rep_ccy, migration 0031).
    SELECT DISTINCT ON (u.instrument_id) u.instrument_id, u.currency
    FROM (
        SELECT fu.instrument_id, split_part(fu.unit, '/', 1) AS currency, count(*) AS n
        FROM fundamentals fu
        WHERE fu.unit ~ '^[A-Z]{3}(/shares)?$'
        GROUP BY 1, 2
    ) u
    ORDER BY u.instrument_id, u.n DESC
),
-- Every TTM flow, one row per company and period end.
ttm AS MATERIALIZED (
    SELECT instrument_id, period_end,
           max(value) FILTER (WHERE grp = 'revenue') AS revenue,
           max(value) FILTER (WHERE grp = 'eps') AS eps,
           max(value) FILTER (WHERE grp = 'net_income') AS net_income,
           max(value) FILTER (WHERE grp = 'op_income') AS op_income,
           max(value) FILTER (WHERE grp = 'gross_profit') AS gross_profit,
           max(value) FILTER (WHERE grp = 'cogs') AS cogs,
           max(value) FILTER (WHERE grp = 'ocf') AS ocf,
           max(value) FILTER (WHERE grp = 'capex') AS capex,
           max(value) FILTER (WHERE grp = 'd_a') AS d_a
    FROM equity_flows_ttm
    GROUP BY instrument_id, period_end
),
grouped AS MATERIALIZED (
    SELECT instrument_id, grp, period_end, period_days, value
    FROM fundamentals_grouped
    WHERE grp IN ('shares_diluted', 'equity', 'cash')
),
-- Diluted shares as equity_metric_series's shares_w reads them: per period
-- end, the shortest period's weighted average.
shares AS (
    SELECT DISTINCT ON (instrument_id, period_end) instrument_id, period_end, value
    FROM grouped
    WHERE grp = 'shares_diluted'
    ORDER BY instrument_id, period_end, period_days ASC
),
-- Book equity and cash: balance-sheet instants (equity_w, cash_w).
book AS MATERIALIZED (
    SELECT DISTINCT ON (instrument_id, period_end) instrument_id, period_end, value
    FROM grouped
    WHERE grp = 'equity' AND period_days = 0
    ORDER BY instrument_id, period_end
),
cash AS (
    SELECT DISTINCT ON (instrument_id, period_end) instrument_id, period_end, value
    FROM grouped
    WHERE grp = 'cash' AND period_days = 0
    ORDER BY instrument_id, period_end
),
debt AS MATERIALIZED (
    SELECT instrument_id, period_end, value FROM equity_debt
),
series(instrument_id, series, period_end, value) AS (
    SELECT instrument_id, 'revenue', period_end, revenue FROM ttm WHERE revenue IS NOT NULL
    UNION ALL
    SELECT instrument_id, 'eps', period_end, eps FROM ttm WHERE eps IS NOT NULL
    UNION ALL
    SELECT instrument_id, 'net_income', period_end, net_income
    FROM ttm WHERE net_income IS NOT NULL
    UNION ALL
    -- equity_fcf_ttm: operating cash flow less capex, both required.
    SELECT instrument_id, 'fcf', period_end, ocf - capex
    FROM ttm WHERE ocf IS NOT NULL AND capex IS NOT NULL
    UNION ALL
    -- ebitda_w: operating income plus D&A, both required.
    SELECT instrument_id, 'ebitda', period_end, op_income + d_a
    FROM ttm WHERE op_income IS NOT NULL AND d_a IS NOT NULL
    UNION ALL
    -- equity_metric_series's gross_margin: gross profit, else revenue less
    -- cost of revenue.
    SELECT instrument_id, 'gross_margin', period_end,
           100.0 * COALESCE(gross_profit, revenue - cogs) / NULLIF(revenue, 0)
    FROM ttm WHERE revenue IS NOT NULL AND (gross_profit IS NOT NULL OR cogs IS NOT NULL)
    UNION ALL
    SELECT instrument_id, 'op_margin', period_end, 100.0 * op_income / NULLIF(revenue, 0)
    FROM ttm WHERE revenue IS NOT NULL AND op_income IS NOT NULL
    UNION ALL
    SELECT instrument_id, 'net_margin', period_end, 100.0 * net_income / NULLIF(revenue, 0)
    FROM ttm WHERE revenue IS NOT NULL AND net_income IS NOT NULL
    UNION ALL
    SELECT instrument_id, 'shares', period_end, value FROM shares
    UNION ALL
    SELECT instrument_id, 'equity', period_end, value FROM book
    UNION ALL
    SELECT instrument_id, 'cash', period_end, value FROM cash
    UNION ALL
    SELECT instrument_id, 'debt', period_end, value FROM debt
    UNION ALL
    -- equity_metric_series's de: debt over book equity on the same day.
    SELECT d.instrument_id, 'de', d.period_end,
           CASE WHEN b.value > 0 THEN d.value / b.value END
    FROM debt d
    JOIN book b ON b.instrument_id = d.instrument_id AND b.period_end = d.period_end
    UNION ALL
    -- Valuation's debt / EBITDA: debt over TTM EBITDA on the same day.
    SELECT d.instrument_id, 'debt_ebitda', d.period_end,
           CASE WHEN t.op_income + t.d_a > 0 THEN d.value / (t.op_income + t.d_a) END
    FROM debt d
    JOIN ttm t ON t.instrument_id = d.instrument_id AND t.period_end = d.period_end
    WHERE t.op_income IS NOT NULL AND t.d_a IS NOT NULL
    UNION ALL
    -- Return on equity: TTM net income over book equity on the same day.
    SELECT t.instrument_id, 'roe', t.period_end,
           CASE WHEN b.value > 0 THEN 100.0 * t.net_income / b.value END
    FROM ttm t
    JOIN book b ON b.instrument_id = t.instrument_id AND b.period_end = t.period_end
    WHERE t.net_income IS NOT NULL
)
SELECT s.instrument_id, s.series, s.period_end, s.value::float8 AS value,
       CASE WHEN s.series IN {money} THEN r.currency END AS currency
FROM series s
LEFT JOIN rep r ON r.instrument_id = s.instrument_id
""".replace("{money}", "(" + ", ".join(f"'{s}'" for s in MONEY_SERIES) + ")")

# One row per company, series and period end. Required by REFRESH ... CONCURRENTLY,
# and serves the screener's "latest point on or before the close" lookups.
SCREENER_FUNDAMENTALS_INDEX = """
CREATE UNIQUE INDEX ux_screener_fundamentals
    ON screener_fundamentals (instrument_id, series, period_end)
"""


def _latest(series: str, alias: str) -> str:
    """The series' latest point on or before the reference day, if it ended
    within STALE_DAYS of it: the window equity_metric_series joins a price to."""
    return f"""
LEFT JOIN LATERAL (
    SELECT f.period_end, f.value, f.currency
    FROM screener_fundamentals f
    WHERE f.instrument_id = p.instrument_id
      AND f.series = '{series}'
      AND f.period_end <= p.ref_date
    ORDER BY f.period_end DESC
    LIMIT 1
) {alias} ON {alias}.period_end > p.ref_date - {STALE_DAYS}"""


def _growth(series: str, alias: str) -> str:
    """Year-on-year % change of a series at its latest point that has one
    335-395 days earlier to compare with (the closest to a year), as
    Valuation's growth chart pairs them: blank unless that earlier figure is
    positive, or if the point ended more than STALE_DAYS before the
    reference day."""
    return f"""
LEFT JOIN LATERAL (
    SELECT c.period_end,
           CASE WHEN b.value > 0 THEN 100.0 * (c.value - b.value) / b.value END AS value
    FROM screener_fundamentals c
    JOIN LATERAL (
        SELECT f.value
        FROM screener_fundamentals f
        WHERE f.instrument_id = c.instrument_id
          AND f.series = c.series
          AND f.period_end BETWEEN c.period_end - 395 AND c.period_end - 335
        ORDER BY abs(f.period_end - (c.period_end - 365)), f.period_end
        LIMIT 1
    ) b ON true
    WHERE c.instrument_id = p.instrument_id
      AND c.series = '{series}'
      AND c.period_end <= p.ref_date
    ORDER BY c.period_end DESC
    LIMIT 1
) {alias} ON {alias}.period_end > p.ref_date - {STALE_DAYS}"""


def _close_before(condition: str) -> str:
    """The last close (> 0) whose date meets `condition`."""
    return f"""(SELECT x.close::float8 FROM prices x
             WHERE x.instrument_id = p.instrument_id AND x.close > 0 AND x.date {condition}
             ORDER BY x.date DESC LIMIT 1)"""


def _close_in(currency: str) -> str:
    """The latest close in `currency` (a reporting currency), as px_rep
    converts it (migration 0028): at the close's own rate, unconverted when
    the currencies match or a rate is missing."""
    return (
        f"p.close * CASE WHEN {currency} IS NULL OR {currency} = p.currency THEN 1"
        f" ELSE COALESCE(fx_usd_rate(p.currency, p.price_date)"
        f" / NULLIF(fx_usd_rate({currency}, p.price_date), 0), 1) END"
    )


EQUITY_SCREENER = f"""
CREATE VIEW equity_screener AS
WITH eq AS (
    SELECT i.id AS instrument_id, i.symbol, i.name, i.currency,
           COALESCE(i.sector, 'Unclassified') AS sector,
           COALESCE(i.region, 'Unclassified') AS region,
           EXISTS (SELECT 1 FROM portfolio_held_instruments h WHERE h.instrument_id = i.id)
               AS held,
           i.in_watchlist AS watched
    FROM instruments i
    WHERE i.kind = 'equity'
),
-- The latest close, and the day the fundamentals are read as of: that close's
-- day, or today for a company with no prices yet.
p AS (
    SELECT e.*, l.date AS price_date, l.close,
           COALESCE(l.date, CURRENT_DATE) AS ref_date
    FROM eq e
    LEFT JOIN LATERAL (
        SELECT x.date, x.close::float8 AS close
        FROM prices x
        WHERE x.instrument_id = e.instrument_id AND x.close > 0
        ORDER BY x.date DESC
        LIMIT 1
    ) l ON true
)
SELECT p.instrument_id, p.symbol, p.name, p.sector, p.region, p.held, p.watched,
       p.currency, p.price_date, p.close,
       100.0 * (p.close / NULLIF(m.m1, 0) - 1) AS chg_1m,
       100.0 * (p.close / NULLIF(m.ytd, 0) - 1) AS chg_ytd,
       100.0 * (p.close / NULLIF(m.y1, 0) - 1) AS chg_1y,
       100.0 * (power(p.close / NULLIF(m.y5, 0), 0.2) - 1) AS chg_5y_annual,
       100.0 * (p.close / NULLIF(m.ma200, 0) - 1) AS vs_ma200,
       100.0 * (1 - p.close / NULLIF(m.hi52, 0)) AS below_high_52w,
       p.close * sh.value AS mcap,
       CASE WHEN eps.value > 0 THEN ({_close_in("eps.currency")}) / eps.value END AS pe,
       CASE WHEN fcf.value > 0
            THEN ({_close_in("fcf.currency")}) * sh.value / fcf.value END AS pfcf,
       CASE WHEN bk.value > 0
            THEN ({_close_in("bk.currency")}) * sh.value / bk.value END AS pb,
       CASE WHEN eb.value > 0
            THEN (({_close_in("eb.currency")}) * sh.value
                  + COALESCE(dt.value, 0) - COALESCE(ca.value, 0)) / eb.value END AS ev_ebitda,
       rg.value AS revenue_growth,
       eg.value AS eps_growth,
       gm.value AS gross_margin,
       om.value AS op_margin,
       nm.value AS net_margin,
       roe.value AS roe,
       de.value AS debt_to_equity,
       debi.value AS debt_to_ebitda,
       sg.value AS shares_change,
       ed.earnings_date AS next_earnings,
       ed.is_estimated AS next_earnings_estimated
FROM p
-- Per company, so it runs once for each row whatever plan the joins get.
LEFT JOIN LATERAL (
    SELECT {_close_before("<= p.price_date - 30")} AS m1,
           {_close_before("< date_trunc('year', p.price_date)::date")} AS ytd,
           {_close_before("<= p.price_date - 365")} AS y1,
           {_close_before("<= (p.price_date - interval '5 years')::date")} AS y5,
           (SELECT max(x.close::float8) FROM prices x
            WHERE x.instrument_id = p.instrument_id AND x.close > 0
              AND x.date > p.price_date - 365 AND x.date <= p.price_date) AS hi52,
           (SELECT CASE WHEN count(*) = 200 THEN avg(s.close) END
            FROM (SELECT x.close::float8 AS close FROM prices x
                  WHERE x.instrument_id = p.instrument_id AND x.close > 0
                    AND x.date <= p.price_date
                  ORDER BY x.date DESC LIMIT 200) s) AS ma200
) m ON true
{_latest("shares", "sh")}
{_latest("eps", "eps")}
{_latest("fcf", "fcf")}
{_latest("equity", "bk")}
{_latest("ebitda", "eb")}
{_latest("debt", "dt")}
{_latest("cash", "ca")}
{_growth("revenue", "rg")}
{_growth("eps", "eg")}
{_growth("shares", "sg")}
{_latest("gross_margin", "gm")}
{_latest("op_margin", "om")}
{_latest("net_margin", "nm")}
{_latest("roe", "roe")}
{_latest("de", "de")}
{_latest("debt_ebitda", "debi")}
LEFT JOIN earnings_dates ed
  ON ed.instrument_id = p.instrument_id AND ed.earnings_date >= CURRENT_DATE
"""


def upgrade() -> None:
    op.execute(SCREENER_FUNDAMENTALS)
    op.execute(SCREENER_FUNDAMENTALS_INDEX)
    op.execute(EQUITY_SCREENER)


def downgrade() -> None:
    op.execute("DROP VIEW equity_screener")
    op.execute("DROP MATERIALIZED VIEW screener_fundamentals")
