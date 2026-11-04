"""Portfolio functions: build FX rates only for the currencies a join can use.

Three Portfolio functions convert amounts at the day's rate by joining
fx_usd_daily on a column: each trade's currency (portfolio_txn_state_in),
each held instrument's listing currency (portfolio_position_daily_in, and the
benchmark's in portfolio_benchmark_daily_in). Joined on a column, the view
can't be narrowed, so every call built every currency's daily calendar (15
pairs since 2004: ~135k rows) to convert a few dozen trades, a dozen
holdings' prices, or one fund's. Each Portfolio panel builds it two to four
times; that was most of what remained of its 0.5-1.5 s after 0032.

Each of those `src` joins now also requires the currency to be one the join
can match: the trades' currencies, the holdings' currencies, or the
benchmark's. That condition is on the view alone, so Postgres pushes it in
and (since 0032) reads only those pairs' prices. Rows that could never join
are no longer built; every result is unchanged. The `dst` joins were already
narrow: the display currency is a constant.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-05

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _load(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replaceable(sql: str) -> str:
    old = "\nCREATE FUNCTION "
    assert sql.count(old) == 1, "function text changed"
    return sql.replace(old, "\nCREATE OR REPLACE FUNCTION ")


def _edit(sql: str, old: str, new: str, what: str) -> str:
    assert sql.count(old) == 1, f"{what} text changed near: {old!r}"
    return _replaceable(sql.replace(old, new))


# Trades convert from their own currency at the trade date.
_TXN_SRC = (
    "    LEFT JOIN fx_usd_daily src\n"
    "      ON src.currency = t.currency AND src.date = t.trade_date\n"
)
_TXN_SRC_NEEDED = (
    _TXN_SRC
    + "     AND src.currency = ANY (ARRAY(\n"
    "         SELECT DISTINCT currency::text FROM portfolio_transactions))\n"
)

# Held instruments' closes convert from their listing currency.
_DAILY_SRC = "    LEFT JOIN fx_usd_daily src ON src.currency = i.currency AND src.date = p.date\n"
_DAILY_SRC_NEEDED = (
    _DAILY_SRC
    + "     AND src.currency = ANY (ARRAY(\n"
    "         SELECT DISTINCT h.currency::text FROM instruments h\n"
    "         WHERE h.id IN (SELECT instrument_id FROM portfolio_transactions)))\n"
)

# The benchmark's closes convert from its listing currency.
_BENCH_SRC_NEEDED = (
    _DAILY_SRC
    + "     AND src.currency = (SELECT b.currency::text FROM instruments b\n"
    "                         WHERE b.symbol = benchmark)\n"
)


def _rev0023() -> ModuleType:
    return _load("0023_portfolio_display_currency.py", "fintracker_migration_0023")


def _rev0025() -> ModuleType:
    return _load("0025_portfolio_benchmark_start.py", "fintracker_migration_0025")


def txn_state_sql() -> str:
    return _edit(str(_rev0023().PORTFOLIO_TXN_STATE_IN), _TXN_SRC, _TXN_SRC_NEEDED,
                 "portfolio_txn_state_in")


def position_daily_sql() -> str:
    return _edit(str(_rev0023().PORTFOLIO_POSITION_DAILY_IN), _DAILY_SRC, _DAILY_SRC_NEEDED,
                 "portfolio_position_daily_in")


def benchmark_daily_sql() -> str:
    return _edit(str(_rev0025().PORTFOLIO_BENCHMARK_DAILY_IN), _DAILY_SRC, _BENCH_SRC_NEEDED,
                 "portfolio_benchmark_daily_in")


def upgrade() -> None:
    op.execute(txn_state_sql())
    op.execute(position_daily_sql())
    op.execute(benchmark_daily_sql())


def downgrade() -> None:
    rev0023, rev0025 = _rev0023(), _rev0025()
    op.execute(_replaceable(str(rev0023.PORTFOLIO_TXN_STATE_IN)))
    op.execute(_replaceable(str(rev0023.PORTFOLIO_POSITION_DAILY_IN)))
    op.execute(_replaceable(str(rev0025.PORTFOLIO_BENCHMARK_DAILY_IN)))
