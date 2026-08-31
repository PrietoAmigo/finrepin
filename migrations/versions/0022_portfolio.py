"""Portfolio / holdings layer: accounts, a transaction ledger, and the views
that turn it into positions, P/L, and a daily portfolio value series.

Holdings are *derived*, never stored: `portfolio_transactions` is the ledger
(one buy or sell per row) and everything else falls out of it, so a corrected
trade fixes every number at once.

Tables
------
- accounts               — where holdings live (broker, exchange, wallet).
- portfolio_transactions — the buy/sell ledger.
- instruments.sector / instruments.region — allocation buckets filled by
  `fintracker.ingest.classify` (Yahoo sector/country for equities, fixed
  labels for crypto and metals).

Views
-----
- portfolio_txn_state    — the ledger walked in order, carrying running
                           quantity, cost basis, and realized P/L per
                           (account, instrument). **Average-cost method**: a
                           buy raises the average, a sell realises
                           proceeds − fees − sold × average and leaves the
                           average untouched. (Mirrored in Python by
                           `fintracker.portfolio.walk_transactions`, which the
                           unit tests exercise.)
- portfolio_position_daily — that state forward-filled onto every calendar day
                           from the first trade to today, joined to the last
                           known close: quantity, cost basis, market value,
                           unrealized and realized P/L per position per day.
- portfolio_positions    — today's slice of the above, plus the labels the
                           allocation panels group by (asset class, sector,
                           region, currency) and per-unit average cost.

Every monetary column is **USD**: trade amounts convert at the trade date's
rate and prices at each bar's rate, both through `fx_usd_daily` (migration
0007), so the dashboards convert USD → display currency with one division.
A currency with no FX history yet falls back to unconverted (rate 1), the same
graceful degradation the fundamentals dashboards use.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The running average cost per unit held *before* the current transaction.
# Quantities stay `numeric` all the way through the walk — selling exactly what
# was bought must land on a hard 0, not on 1e-17, or a closed position would
# read as an open one everywhere `quantity <> 0` is tested. Money is float8,
# like every other view here, because that is what Grafana plots.
_ZERO_QTY = "0::numeric"
# Guarded so an over-sell (more units than held — a data-entry slip) can't
# divide by zero; it simply realises the full proceeds and leaves basis at 0.
_AVG_BEFORE = (
    "CASE WHEN w.quantity_after > 0 THEN w.cost_after / w.quantity_after"
    " ELSE 0::float8 END"
)
# Realized P/L booked by one sell: proceeds − fees − (units sold × average cost).
_REALIZED_STEP = (
    "o.quantity * o.price_usd - o.fees_usd"
    f" - LEAST(o.quantity, GREATEST(w.quantity_after, {_ZERO_QTY})) * " + _AVG_BEFORE
)

PORTFOLIO_TXN_STATE = f"""
CREATE VIEW portfolio_txn_state AS
WITH RECURSIVE ordered AS (
    SELECT t.id, t.account_id, t.instrument_id, t.trade_date, t.side,
           t.quantity,
           t.price::float8 * COALESCE(fx.usd_rate, 1) AS price_usd,
           t.fees::float8 * COALESCE(fx.usd_rate, 1) AS fees_usd,
           row_number() OVER (PARTITION BY t.account_id, t.instrument_id
                              ORDER BY t.trade_date, t.id) AS rn
    FROM portfolio_transactions t
    LEFT JOIN fx_usd_daily fx
      ON fx.currency = t.currency AND fx.date = t.trade_date
),
walk AS (
    -- First trade of each position: nothing held before it.
    SELECT o.id, o.account_id, o.instrument_id, o.trade_date, o.side, o.rn,
           o.quantity, o.price_usd, o.fees_usd,
           CASE WHEN o.side = 'buy' THEN o.quantity ELSE -o.quantity END
               AS quantity_after,
           CASE WHEN o.side = 'buy' THEN o.quantity * o.price_usd + o.fees_usd
                ELSE 0::float8 END AS cost_after,
           CASE WHEN o.side = 'buy' THEN 0::float8
                ELSE o.quantity * o.price_usd - o.fees_usd END AS realized_delta,
           CASE WHEN o.side = 'buy' THEN 0::float8
                ELSE o.quantity * o.price_usd - o.fees_usd END AS realized_after
    FROM ordered o
    WHERE o.rn = 1
    UNION ALL
    SELECT o.id, o.account_id, o.instrument_id, o.trade_date, o.side, o.rn,
           o.quantity, o.price_usd, o.fees_usd,
           w.quantity_after
               + CASE WHEN o.side = 'buy' THEN o.quantity ELSE -o.quantity END,
           CASE WHEN o.side = 'buy'
                THEN w.cost_after + o.quantity * o.price_usd + o.fees_usd
                ELSE GREATEST(w.quantity_after - o.quantity, {_ZERO_QTY})
                     * {_AVG_BEFORE}
           END,
           CASE WHEN o.side = 'buy' THEN 0::float8 ELSE {_REALIZED_STEP} END,
           w.realized_after
               + CASE WHEN o.side = 'buy' THEN 0::float8 ELSE {_REALIZED_STEP} END
    FROM walk w
    JOIN ordered o
      ON o.account_id = w.account_id
     AND o.instrument_id = w.instrument_id
     AND o.rn = w.rn + 1
)
SELECT id, account_id, instrument_id, trade_date, side, rn,
       quantity, price_usd, fees_usd,
       quantity_after, cost_after, realized_delta, realized_after
FROM walk
"""

# Forward fill on two independent clocks — position state changes only on trade
# days, prices only on trading days — via the count-of-non-nulls grouping trick
# already used by fx_usd_daily.
PORTFOLIO_POSITION_DAILY = """
CREATE VIEW portfolio_position_daily AS
WITH eod AS (
    SELECT DISTINCT ON (account_id, instrument_id, trade_date)
           account_id, instrument_id, trade_date AS date,
           quantity_after, cost_after, realized_after
    FROM portfolio_txn_state
    ORDER BY account_id, instrument_id, trade_date, rn DESC
),
span AS (
    SELECT account_id, instrument_id,
           generate_series(min(date), CURRENT_DATE, interval '1 day')::date AS date
    FROM eod
    GROUP BY account_id, instrument_id
),
px AS (
    SELECT p.instrument_id, p.date,
           p.close::float8 * COALESCE(fx.usd_rate, 1) AS price_usd
    FROM prices p
    JOIN instruments i ON i.id = p.instrument_id
    LEFT JOIN fx_usd_daily fx ON fx.currency = i.currency AND fx.date = p.date
    WHERE EXISTS (
        SELECT 1 FROM portfolio_transactions t WHERE t.instrument_id = p.instrument_id
    )
),
joined AS (
    SELECT s.account_id, s.instrument_id, s.date,
           e.quantity_after, e.cost_after, e.realized_after, x.price_usd,
           count(e.quantity_after) OVER w AS sgrp,
           count(x.price_usd) OVER w AS pgrp
    FROM span s
    LEFT JOIN eod e
      ON e.account_id = s.account_id
     AND e.instrument_id = s.instrument_id
     AND e.date = s.date
    LEFT JOIN px x ON x.instrument_id = s.instrument_id AND x.date = s.date
    WINDOW w AS (PARTITION BY s.account_id, s.instrument_id ORDER BY s.date)
),
filled AS (
    SELECT account_id, instrument_id, date,
           max(quantity_after) OVER (PARTITION BY account_id, instrument_id, sgrp)
               AS quantity,
           max(cost_after) OVER (PARTITION BY account_id, instrument_id, sgrp)
               AS cost_basis,
           max(realized_after) OVER (PARTITION BY account_id, instrument_id, sgrp)
               AS realized,
           max(price_usd) OVER (PARTITION BY account_id, instrument_id, pgrp)
               AS price_usd
    FROM joined
)
SELECT account_id, instrument_id, date, quantity, cost_basis, realized, price_usd,
       quantity * price_usd AS market_value,
       quantity * price_usd - cost_basis AS unrealized
FROM filled
"""

PORTFOLIO_POSITIONS = """
CREATE VIEW portfolio_positions AS
SELECT d.account_id, a.name AS account, d.instrument_id,
       i.symbol, i.name AS instrument, i.kind, i.currency,
       CASE i.kind
            WHEN 'equity' THEN 'Equities'
            WHEN 'crypto' THEN 'Crypto'
            WHEN 'metal'  THEN 'Precious metals'
            WHEN 'index'  THEN 'Funds & indexes'
            WHEN 'forex'  THEN 'Cash & FX'
            ELSE initcap(i.kind)
       END AS asset_class,
       COALESCE(i.sector, 'Unclassified') AS sector,
       COALESCE(i.region, 'Unclassified') AS region,
       d.date, d.quantity, d.cost_basis, d.price_usd, d.market_value,
       d.unrealized, d.realized,
       CASE WHEN d.quantity <> 0 THEN d.cost_basis / d.quantity END AS avg_cost,
       CASE WHEN d.cost_basis > 0 THEN 100.0 * d.unrealized / d.cost_basis END
           AS unrealized_pct
FROM portfolio_position_daily d
JOIN accounts a ON a.id = d.account_id
JOIN instruments i ON i.id = d.instrument_id
WHERE d.date = CURRENT_DATE
"""


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("broker", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), server_default="EUR", nullable=False),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "portfolio_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 10), nullable=False),
        sa.Column("price", sa.Numeric(30, 10), nullable=False),
        sa.Column("fees", sa.Numeric(20, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_portfolio_txn_side"),
        sa.CheckConstraint("quantity > 0", name="ck_portfolio_txn_quantity"),
    )
    op.create_index(
        "ix_portfolio_transactions_account_id", "portfolio_transactions", ["account_id"]
    )
    op.create_index(
        "ix_portfolio_transactions_instrument_id", "portfolio_transactions", ["instrument_id"]
    )
    op.create_index(
        "ix_portfolio_txn_position",
        "portfolio_transactions",
        ["account_id", "instrument_id", "trade_date"],
    )
    op.create_index("ix_portfolio_txn_date", "portfolio_transactions", ["trade_date"])

    op.add_column("instruments", sa.Column("sector", sa.String(length=64), nullable=True))
    op.add_column("instruments", sa.Column("region", sa.String(length=32), nullable=True))

    op.execute(PORTFOLIO_TXN_STATE)
    op.execute(PORTFOLIO_POSITION_DAILY)
    op.execute(PORTFOLIO_POSITIONS)


def downgrade() -> None:
    op.execute("DROP VIEW portfolio_positions")
    op.execute("DROP VIEW portfolio_position_daily")
    op.execute("DROP VIEW portfolio_txn_state")
    op.drop_column("instruments", "region")
    op.drop_column("instruments", "sector")
    op.drop_table("portfolio_transactions")
    op.drop_table("accounts")
