"""Every dashboard: queries compute what they show, not every instrument's data.

Checks on the dashboard JSON, without a database:
- no panel reads fx_usd_daily, which builds every currency's gap-filled daily
  calendar; panels convert with fx_usd_rate (two index lookups per point) or
  read the latest close of each FX pair;
- the per-instrument views are filtered with
  `instrument_id = (SELECT id FROM instruments WHERE symbol = ...)`, which
  Postgres pushes into them; a join on symbol can't be.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DASHBOARDS = sorted(
    (Path(__file__).resolve().parents[1] / "grafana" / "dashboards").glob("*.json")
)
PER_INSTRUMENT_VIEWS = (
    "equity_metric_series",
    "equity_statement_matrix",
    "equity_statement_annual",
    "equity_statement_quarterly",
    "equity_statement_periods",
    "equity_statement_period_matrix",
)
PUSHED_DOWN = r"instrument_id = \(SELECT id FROM instruments WHERE symbol = '\$\w+'\)"


def _panels(ps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for p in ps:
        yield p
        yield from _panels(p.get("panels", []) or [])


def _queries(path: Path) -> Iterator[tuple[str, str]]:
    dashboard = json.loads(path.read_text())
    for var in dashboard.get("templating", {}).get("list", []):
        if var.get("type") == "query":
            query = var["query"]
            yield f"{path.name} variable {var['name']}", (
                query if isinstance(query, str) else query.get("rawSql", "")
            )
    for panel in _panels(dashboard.get("panels", [])):
        for target in panel.get("targets", []) or []:
            if target.get("rawSql"):
                yield f"{path.name} panel {panel.get('id')} {panel.get('title')}", target["rawSql"]


ALL_QUERIES = [q for path in DASHBOARDS for q in _queries(path)]


def test_found_the_dashboards() -> None:
    assert len(DASHBOARDS) >= 7
    assert len(ALL_QUERIES) >= 60


@pytest.mark.parametrize(("where", "sql"), ALL_QUERIES, ids=[w for w, _ in ALL_QUERIES])
def test_no_query_builds_the_fx_calendar(where: str, sql: str) -> None:
    assert "fx_usd_daily" not in sql, where


@pytest.mark.parametrize(("where", "sql"), ALL_QUERIES, ids=[w for w, _ in ALL_QUERIES])
def test_per_instrument_views_are_filtered_by_instrument_id(where: str, sql: str) -> None:
    for view in PER_INSTRUMENT_VIEWS:
        for alias in re.findall(rf"\bFROM {view} (\w+)", sql):
            assert re.search(rf"\b{alias}\.{PUSHED_DOWN}", sql), (
                f"{where}: {view} {alias} isn't filtered by instrument_id"
            )
            joined = rf"JOIN instruments \w+ ON \w+\.id = {alias}\.instrument_id"
            assert not re.search(joined, sql), (
                f"{where}: {view} filtered through a join on instruments"
            )


def test_positions_daily_change_reads_only_the_holdings() -> None:
    sql = dict(ALL_QUERIES)["portfolio.json panel 13 Positions — $display_currency"]
    assert "FROM prices p WHERE p.instrument_id IN (SELECT instrument_id FROM held)" in sql
