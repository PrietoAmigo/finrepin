"""Migrations 0031 (reporting currency counted once) and 0032 (fx_usd_daily).

Checks on the generated SQL text, without a database: 0031 changes only
0029's `ccy` block and leaves no correlated lookup over fundamentals in it;
0032 keeps fx_usd_daily's columns and drops the calendar join.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load(filename: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0029() -> ModuleType:
    return _load("0029_metric_series_per_instrument.py", "migration_0029_for_0031")


@pytest.fixture(scope="module")
def rev0031() -> ModuleType:
    return _load("0031_reporting_currency_once.py", "migration_0031_under_test")


@pytest.fixture(scope="module")
def rev0032() -> ModuleType:
    return _load("0032_fx_usd_daily_forward_fill.py", "migration_0032_under_test")


def test_0031_changes_only_the_currency_lookup(rev0029: ModuleType, rev0031: ModuleType) -> None:
    old, new = rev0029.metric_series_sql(), rev0031.metric_series_sql()
    assert new.replace(rev0031.CCY, rev0029.CCY) == old


def test_0031_reporting_currency_is_not_correlated(rev0031: ModuleType) -> None:
    # A LATERAL (or any reference to instruments) inside the counting subquery
    # makes it rerun on every rescan, once per daily price.
    rep = rev0031.CCY.split("\nccy AS NOT MATERIALIZED (")[0]
    assert "FROM fundamentals fu" in rep
    assert "LATERAL" not in rev0031.CCY
    assert "instruments" not in rep
    assert "LEFT JOIN rep_ccy rep ON rep.instrument_id = i.id" in rev0031.CCY


def test_0031_every_cte_is_inlined(rev0031: ModuleType) -> None:
    sql = rev0031.metric_series_sql()
    assert "rep_ccy AS NOT MATERIALIZED (" in sql
    assert "ccy AS NOT MATERIALIZED (" in sql
    assert re.findall(r"^(?:WITH )?\w+ AS \($", sql, flags=re.MULTILINE) == []


def _select_columns(view_sql: str) -> list[str]:
    """Output column names of the view's first top-level SELECT branch."""
    body = view_sql.split("\nSELECT ", 1)[1].split("\nFROM ", 1)[0]
    items, depth, current = [], 0, ""
    for ch in body:  # split on commas outside parentheses
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += ch
    items.append(current)
    return [re.split(r"\s+AS\s+|\.", c.strip())[-1] for c in items]


def test_0032_keeps_the_columns(rev0032: ModuleType) -> None:
    old = rev0032.fx_usd_daily_0007()
    assert _select_columns(rev0032.FX_USD_DAILY) == ["currency", "date", "usd_rate"]
    assert _select_columns(old) == ["currency", "date", "usd_rate"]
    assert old.startswith("\nCREATE OR REPLACE VIEW fx_usd_daily AS")


def test_0032_reads_each_close_once_without_a_calendar_join(rev0032: ModuleType) -> None:
    sql = rev0032.FX_USD_DAILY
    assert sql.count("JOIN prices pr") == 1
    assert "LEAD(pr.date) OVER (PARTITION BY pa.currency ORDER BY pr.date)" in sql
    # 0007's calendar, joined to the closes and gap-numbered with a count.
    assert "cal AS" not in sql
    assert "COUNT(" not in sql.upper()
    # The last close still fills up to today, the USD row is unchanged.
    assert "GREATEST(c.date, CURRENT_DATE)" in sql
    assert "SELECT 'USD', gs::date, 1.0" in sql
