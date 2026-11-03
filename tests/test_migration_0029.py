"""Migration 0029 builds its views from 0008/0012/0028's text; guard the result.

The point of 0029 is that an instrument filter reaches every part of the views
(no CTE materialized for all instruments) and that the reporting currency is
looked up per instrument, never per price row. These checks run on the SQL
text, without a database.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


@pytest.fixture(scope="module")
def rev0029() -> ModuleType:
    path = MIGRATIONS / "0029_metric_series_per_instrument.py"
    spec = importlib.util.spec_from_file_location("migration_0029_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_cte_of_the_metric_view_is_inlined(rev0029: ModuleType) -> None:
    sql = rev0029.metric_series_sql()
    ctes = ["ccy", "px_rep", "eps_w", "fcf_w", "shares_w", "equity_w", "cash_w", "debt_w",
            "ebitda_w"]
    for name in ctes:
        assert f"{name} AS NOT MATERIALIZED (" in sql, name
    # No CTE left that Postgres may materialize (WINDOW w AS (...) is not one).
    assert re.findall(r"^(?:WITH )?\w+ AS \($", sql, flags=re.MULTILINE) == []


def test_ratios_read_the_converted_close(rev0029: ModuleType) -> None:
    sql = rev0029.metric_series_sql()
    assert sql.count("FROM px_rep p") == 4  # P/E, P/FCF, EV/EBITDA, P/B
    # Price and MCap stay in the listing currency.
    assert "'price', 'Price', p.date::timestamptz,\n       p.close::float8," in sql


def test_reporting_currency_is_not_looked_up_per_price(rev0029: ModuleType) -> None:
    px_rep = rev0029.PX_REP
    assert "instrument_currencies" not in px_rep
    assert "fundamentals" not in px_rep  # only ccy reads fundamentals, once per instrument


def test_quarterly_flows_are_inlined(rev0029: ModuleType) -> None:
    sql = rev0029.flows_quarterly_sql()
    for name in ("quarterly", "annual", "derived_q4"):
        assert f"{name} AS NOT MATERIALIZED (" in sql, name
