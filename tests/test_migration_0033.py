"""Migration 0033: the Portfolio functions build FX rates only where a join can use them.

Checks on the generated SQL text, without a database: each function differs
from 0023/0025 only by the condition on its `src` join, every fx_usd_daily
join is narrowed (to a constant or to the currencies it can match), and the
downgrade restores the old text.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from fintracker.models import Instrument, PortfolioTransaction

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load(filename: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0033() -> ModuleType:
    return _load("0033_portfolio_fx_needed_currencies.py", "migration_0033_under_test")


@pytest.fixture(scope="module")
def originals() -> dict[str, str]:
    rev0023 = _load("0023_portfolio_display_currency.py", "migration_0023_for_0033")
    rev0025 = _load("0025_portfolio_benchmark_start.py", "migration_0025_for_0033")
    return {
        "txn_state_sql": rev0023.PORTFOLIO_TXN_STATE_IN,
        "position_daily_sql": rev0023.PORTFOLIO_POSITION_DAILY_IN,
        "benchmark_daily_sql": rev0025.PORTFOLIO_BENCHMARK_DAILY_IN,
    }


FUNCTIONS = ["txn_state_sql", "position_daily_sql", "benchmark_daily_sql"]
NARROWED = {
    "txn_state_sql": "SELECT DISTINCT currency::text FROM portfolio_transactions",
    "position_daily_sql": "WHERE h.id IN (SELECT instrument_id FROM portfolio_transactions)",
    "benchmark_daily_sql": "WHERE b.symbol = benchmark",
}


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_only_the_src_join_changes(
    rev0033: ModuleType, originals: dict[str, str], fn: str
) -> None:
    new = getattr(rev0033, fn)()
    old = originals[fn].replace("\nCREATE FUNCTION ", "\nCREATE OR REPLACE FUNCTION ", 1)
    added = {
        "txn_state_sql": rev0033._TXN_SRC_NEEDED[len(rev0033._TXN_SRC):],
        "position_daily_sql": rev0033._DAILY_SRC_NEEDED[len(rev0033._DAILY_SRC):],
        "benchmark_daily_sql": rev0033._BENCH_SRC_NEEDED[len(rev0033._DAILY_SRC):],
    }[fn]
    assert new.count(added) == 1
    assert new.replace(added, "") == old
    assert NARROWED[fn] in added


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_every_fx_join_is_narrowed(rev0033: ModuleType, fn: str) -> None:
    sql = getattr(rev0033, fn)()
    joins = re.findall(r"LEFT JOIN fx_usd_daily (\w+)\s+ON (.*?)(?=\n    \S|\n\))", sql, re.S)
    assert {alias for alias, _ in joins} == {"src", "dst"}
    for alias, on in joins:
        if alias == "dst":
            assert "dst.currency = ccy" in on  # the display currency: a constant
        else:
            assert re.search(r"AND src\.currency = (ANY \(ARRAY\(|\(SELECT)", on), on


def test_the_narrowing_reads_no_parameter_as_a_column(rev0033: ModuleType) -> None:
    # In a SQL function a same-named column wins over a parameter.
    columns = {c.name for t in (Instrument, PortfolioTransaction) for c in t.__table__.columns}
    assert "benchmark" not in columns
    assert "ccy" not in columns


def test_downgrade_restores_0023_and_0025(
    rev0033: ModuleType, originals: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[str] = []
    monkeypatch.setattr(rev0033, "op", type("Op", (), {"execute": staticmethod(executed.append)}))
    rev0033.downgrade()
    assert executed == [
        originals[fn].replace("\nCREATE FUNCTION ", "\nCREATE OR REPLACE FUNCTION ", 1)
        for fn in FUNCTIONS
    ]
