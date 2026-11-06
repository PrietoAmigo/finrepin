"""Migration 0030 and the Financial Statements dashboard: guard the fast path.

The statement tables are fast only while (a) the partial index matches the
statement view's fiscal-year-end check, (b) no CTE of the matrix view is
materialized for every instrument, and (c) the dashboard filters by
instrument_id (which Postgres pushes into the views) and converts through
fx_usd_rate rather than fx_usd_daily. These checks run on the SQL text,
without a database.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fintracker.models import Fundamental, Instrument, Price

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "versions"
DASHBOARD = ROOT / "grafana" / "dashboards" / "financial-statements.json"


def _load(filename: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0030() -> ModuleType:
    return _load("0030_statement_views_per_instrument.py", "migration_0030_under_test")


@pytest.fixture(scope="module")
def rev0007() -> ModuleType:
    return _load("0007_currency_views.py", "migration_0007_under_test")


def test_index_predicate_matches_the_fiscal_year_end_check(
    rev0030: ModuleType, rev0007: ModuleType
) -> None:
    annual = rev0007._annual_view(with_currency=True)
    # The EXISTS reads fy_ends, whose filter must be the index's predicate for
    # the planner to probe the partial index.
    fy_ends = annual.split("fy_ends AS (", 1)[1].split("),", 1)[0]
    assert f"WHERE {rev0030.ANNUAL_PREDICATE}" in fy_ends
    assert "FROM fy_ends e" in annual
    assert "ON fundamentals (instrument_id, period_end)" in rev0030.ANNUAL_END_INDEX
    assert f"WHERE {rev0030.ANNUAL_PREDICATE}" in rev0030.ANNUAL_END_INDEX


def test_model_declares_the_same_partial_index(rev0030: ModuleType) -> None:
    index = next(
        i for i in Fundamental.__table__.indexes if i.name == "ix_fundamentals_annual_end"
    )
    assert [c.name for c in index.columns] == ["instrument_id", "period_end"]
    assert str(index.dialect_options["postgresql"]["where"]) == rev0030.ANNUAL_PREDICATE


def test_no_matrix_cte_is_materialized(rev0030: ModuleType) -> None:
    sql = rev0030.matrix_view_sql()
    assert "lines AS NOT MATERIALIZED (" in sql
    # A CTE without NOT MATERIALIZED is inlined only while it is read once.
    for name in re.findall(r"^(?:WITH )?(\w+) AS \($", sql, flags=re.MULTILINE):
        reads = re.findall(rf"\b(?:FROM|JOIN) {name}\b", sql)
        assert len(reads) == 1, f"{name} is read {len(reads)} times"


def test_fx_usd_rate_parameters_do_not_shadow_columns(rev0030: ModuleType) -> None:
    # In a SQL function a column of the same name wins over a parameter, so
    # `currency` would silently read instruments.currency.
    signature = re.search(r"fx_usd_rate\((.*?)\)", rev0030.FX_USD_RATE)
    assert signature is not None
    params = [p.split()[0] for p in signature.group(1).split(",")]
    assert params == ["ccy", "day"]
    columns = {c.name for t in (Instrument, Price) for c in t.__table__.columns}
    assert columns.isdisjoint(params)


def _queries(dashboard: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for var in dashboard["templating"]["list"]:
        if var["type"] == "query":
            yield f"variable {var['name']}", var["query"]

    def panels(ps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for p in ps:
            yield p
            yield from panels(p.get("panels", []))

    for panel in panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            yield f"panel {panel['title']}", target["rawSql"]


def test_dashboard_filters_by_instrument_id_and_converts_by_lookup() -> None:
    statement_queries = [
        (where, sql)
        for where, sql in _queries(json.loads(DASHBOARD.read_text()))
        if "equity_statement_" in sql
    ]
    variables = [w for w, _ in statement_queries if w.startswith("variable")]
    assert variables == ["variable fiscal_year", "variable since", "variable heading"]
    assert len(statement_queries) > 4  # highlights, trends and the statement tables
    pushed_down = "instrument_id = (SELECT id FROM instruments WHERE symbol = '$ticker')"
    for where, sql in statement_queries:
        assert pushed_down in sql, where
        assert "JOIN instruments" not in sql, where
        assert "fx_usd_daily" not in sql, where
    for where, sql in statement_queries:
        if where.startswith("variable"):
            continue
        # Each cell converts at its fiscal-year-end rate, by lookup.
        assert re.search(r"fx_usd_rate\((\w+)\.currency, \1\.period_end\)", sql), where
        assert re.search(r"fx_usd_rate\('\$display_currency', \w+\.period_end\)", sql), where
