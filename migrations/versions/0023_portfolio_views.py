"""Portfolio SQL views: value over time, positions, allocation, P/L, drawdown.

Layered plain (non-materialized) views over `holdings`/`accounts`/`prices`,
following the same gap-fill and FX-conversion techniques migration 0007 used
for `fx_usd_daily`:

- prices_daily_filled    — one row per (instrument, calendar day) with the
                            close forward-filled across weekends/holidays,
                            same "count-of-non-nulls" window trick as
                            `fx_usd_daily`. Feeds the value-over-time chart so
                            it doesn't gap on non-trading days.
- portfolio_lot_value_daily — per lot, per day, the position's value in its
                            native currency and in USD (via `fx_usd_daily`).
                            A lot contributes its full original `quantity`
                            from `acquired_at` through the day before it is
                            fully sold (`quantity_sold = quantity`,
                            `last_sold_at` set), then drops out — so closed
                            positions still show their historical
                            contribution. A *partial* sale does not shift the
                            quantity used here (its exact date is not
                            otherwise recorded per-lot): the lot keeps
                            contributing its pre-sale size until fully closed
                            or the present day. Close a lot out fully (rather
                            than trickling partial sells) for an exact chart.
- portfolio_value_daily  — the above summed to one total USD value per day.
- portfolio_drawdown_daily — running all-time-high and % drawdown from it,
                            over `portfolio_value_daily`.
- portfolio_positions    — currently open lots (quantity_sold < quantity)
                            aggregated per (instrument, account): quantity,
                            cost basis (converted to USD at each lot's
                            acquisition-date FX rate), latest market value
                            (converted at the latest available FX rate),
                            unrealized P/L, and % weight of the whole
                            portfolio. Carries `asset_class` (instrument
                            kind), `price_currency`, `sector`, `region` for
                            the allocation panels — group/sum
                            `market_value_usd` by whichever of those.
- portfolio_realized_pl  — one row per lot with any sold quantity: proceeds,
                            the matching slice of cost basis, and realized
                            P/L, in both the lot's native currency and USD
                            (converted at `last_sold_at`'s FX rate).

All USD amounts fall back to the native amount unconverted when a currency's
FX history hasn't been ingested yet (rate treated as 1), matching
`fx_usd_daily`'s own fallback — see migration 0007.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-31

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICES_DAILY_FILLED = """
CREATE VIEW prices_daily_filled AS
WITH cal AS (
    SELECT instrument_id,
           generate_series(min(date), GREATEST(max(date), CURRENT_DATE),
                           interval '1 day')::date AS date
    FROM prices
    GROUP BY instrument_id
),
joined AS (
    SELECT c.instrument_id, c.date, p.close,
           COUNT(p.close) OVER (PARTITION BY c.instrument_id ORDER BY c.date) AS grp
    FROM cal c
    LEFT JOIN prices p ON p.instrument_id = c.instrument_id AND p.date = c.date
)
SELECT instrument_id, date,
       MAX(close) OVER (PARTITION BY instrument_id, grp) AS close
FROM joined
"""

PORTFOLIO_LOT_VALUE_DAILY = """
CREATE VIEW portfolio_lot_value_daily AS
WITH lots AS (
    SELECT h.id AS holding_id, h.instrument_id, h.account_id, h.quantity,
           h.acquired_at,
           LEAST(
               CASE WHEN h.quantity_sold >= h.quantity
                    THEN COALESCE(h.last_sold_at, h.acquired_at) - 1
                    ELSE CURRENT_DATE
               END,
               CURRENT_DATE
           ) AS end_date
    FROM holdings h
    WHERE h.acquired_at <= CURRENT_DATE
),
daily AS (
    SELECT l.holding_id, l.instrument_id, l.account_id, l.quantity, d::date AS date
    FROM lots l, generate_series(l.acquired_at, l.end_date, interval '1 day') AS d
    WHERE l.end_date >= l.acquired_at
)
SELECT d.date, d.holding_id, d.instrument_id, d.account_id,
       d.quantity, pf.close AS price_native, i.currency AS price_currency,
       d.quantity * pf.close AS value_native,
       d.quantity * pf.close * COALESCE(fx.usd_rate, 1) AS value_usd
FROM daily d
JOIN instruments i ON i.id = d.instrument_id
LEFT JOIN prices_daily_filled pf ON pf.instrument_id = d.instrument_id AND pf.date = d.date
LEFT JOIN fx_usd_daily fx ON fx.currency = i.currency AND fx.date = d.date
"""

PORTFOLIO_VALUE_DAILY = """
CREATE VIEW portfolio_value_daily AS
SELECT date, SUM(value_usd) AS value_usd
FROM portfolio_lot_value_daily
WHERE value_usd IS NOT NULL
GROUP BY date
"""

PORTFOLIO_DRAWDOWN_DAILY = """
CREATE VIEW portfolio_drawdown_daily AS
SELECT date, value_usd,
       MAX(value_usd) OVER (ORDER BY date) AS ath_usd,
       CASE WHEN MAX(value_usd) OVER (ORDER BY date) > 0
            THEN 100.0 * (value_usd - MAX(value_usd) OVER (ORDER BY date))
                 / MAX(value_usd) OVER (ORDER BY date)
       END AS drawdown_pct
FROM portfolio_value_daily
"""

PORTFOLIO_POSITIONS = """
CREATE VIEW portfolio_positions AS
WITH open_lots AS (
    SELECT h.instrument_id, h.account_id,
           (h.quantity - h.quantity_sold) AS quantity_open,
           h.cost_basis * (h.quantity - h.quantity_sold) / NULLIF(h.quantity, 0)
               AS cost_basis_open,
           h.currency, h.acquired_at
    FROM holdings h
    WHERE h.quantity_sold < h.quantity
),
cost_fx AS (
    SELECT ol.instrument_id, ol.account_id, ol.quantity_open,
           ol.cost_basis_open * COALESCE(fx.usd_rate, 1) AS cost_basis_usd
    FROM open_lots ol
    LEFT JOIN fx_usd_daily fx ON fx.currency = ol.currency AND fx.date = ol.acquired_at
),
agg AS (
    SELECT instrument_id, account_id,
           SUM(quantity_open) AS quantity_open,
           SUM(cost_basis_usd) AS cost_basis_usd
    FROM cost_fx
    GROUP BY instrument_id, account_id
),
latest_price AS (
    SELECT DISTINCT ON (instrument_id) instrument_id, date, close
    FROM prices
    ORDER BY instrument_id, date DESC
),
positions AS (
    SELECT a.instrument_id, a.account_id, acc.name AS account_name,
           i.symbol, i.name, i.kind AS asset_class, i.currency AS price_currency,
           i.sector, i.region,
           a.quantity_open, a.cost_basis_usd,
           lp.close AS latest_price_native, lp.date AS latest_price_date,
           a.quantity_open * lp.close * COALESCE(fx.usd_rate, 1) AS market_value_usd
    FROM agg a
    JOIN instruments i ON i.id = a.instrument_id
    JOIN accounts acc ON acc.id = a.account_id
    LEFT JOIN latest_price lp ON lp.instrument_id = a.instrument_id
    LEFT JOIN fx_usd_daily fx ON fx.currency = i.currency AND fx.date = lp.date
)
SELECT instrument_id, account_id, account_name, symbol, name, asset_class,
       price_currency, sector, region, quantity_open, cost_basis_usd,
       latest_price_native, latest_price_date, market_value_usd,
       market_value_usd - cost_basis_usd AS unrealized_pl_usd,
       CASE WHEN cost_basis_usd > 0
            THEN 100.0 * (market_value_usd - cost_basis_usd) / cost_basis_usd
       END AS unrealized_pl_pct,
       100.0 * market_value_usd / NULLIF(SUM(market_value_usd) OVER (), 0) AS weight_pct
FROM positions
"""

PORTFOLIO_REALIZED_PL = """
CREATE VIEW portfolio_realized_pl AS
SELECT h.id AS holding_id, h.instrument_id, h.account_id, acc.name AS account_name,
       i.symbol, i.name, i.kind AS asset_class, h.currency,
       h.acquired_at, h.last_sold_at,
       h.quantity_sold,
       h.proceeds,
       (h.cost_basis * h.quantity_sold / NULLIF(h.quantity, 0)) AS cost_basis_sold,
       (h.proceeds - h.cost_basis * h.quantity_sold / NULLIF(h.quantity, 0)) AS realized_pl,
       h.proceeds * COALESCE(fx.usd_rate, 1) AS proceeds_usd,
       (h.cost_basis * h.quantity_sold / NULLIF(h.quantity, 0)) * COALESCE(fx.usd_rate, 1)
           AS cost_basis_sold_usd,
       (h.proceeds - h.cost_basis * h.quantity_sold / NULLIF(h.quantity, 0))
           * COALESCE(fx.usd_rate, 1) AS realized_pl_usd
FROM holdings h
JOIN instruments i ON i.id = h.instrument_id
JOIN accounts acc ON acc.id = h.account_id
LEFT JOIN fx_usd_daily fx ON fx.currency = h.currency AND fx.date = h.last_sold_at
WHERE h.quantity_sold > 0
"""

_VIEWS = (
    ("prices_daily_filled", PRICES_DAILY_FILLED),
    ("portfolio_lot_value_daily", PORTFOLIO_LOT_VALUE_DAILY),
    ("portfolio_value_daily", PORTFOLIO_VALUE_DAILY),
    ("portfolio_drawdown_daily", PORTFOLIO_DRAWDOWN_DAILY),
    ("portfolio_positions", PORTFOLIO_POSITIONS),
    ("portfolio_realized_pl", PORTFOLIO_REALIZED_PL),
)


def upgrade() -> None:
    for _, sql in _VIEWS:
        op.execute(sql)


def downgrade() -> None:
    for name, _ in reversed(_VIEWS):
        op.execute(f"DROP VIEW {name}")
