"""The Financial Statements dashboard's layout rules, checked on its JSON.

Highlights compare the latest selected fiscal year with the year before; the
statement tables put the latest year first and show money in millions (a
fixed scale, so a row never switches between Mil and Bil); charts use only
the validated categorical palette.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DASHBOARD = (
    Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "financial-statements.json"
)
# Validated categorical slots (blue, orange, aqua) on Grafana's dark panel surface.
PALETTE = {"#3987e5", "#d95926", "#199e70"}


def _panels(ps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for p in ps:
        yield p
        yield from _panels(p.get("panels", []) or [])


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text())


@pytest.fixture(scope="module")
def panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_panels(dashboard["panels"]))


def _sql(panel: dict[str, Any]) -> str:
    return str(panel["targets"][0]["rawSql"])


def test_sections_in_reading_order(panels: list[dict[str, Any]]) -> None:
    rows = [p["title"] for p in panels if p["type"] == "row"]
    assert rows == [
        "Highlights — FY$latest_fy vs the year before, $display_currency",
        "Trends",
        "Income statement",
        "Balance sheet",
        "Cash flow statement",
    ]


def test_latest_fy_variable_is_hidden(dashboard: dict[str, Any]) -> None:
    latest = next(v for v in dashboard["templating"]["list"] if v["name"] == "latest_fy")
    assert latest["hide"] == 2
    assert "max(a.fiscal_year)" in latest["query"] and "IN ($fiscal_year)" in latest["query"]


def test_highlights_compare_with_the_year_before(panels: list[dict[str, Any]]) -> None:
    """Each tile's arrow is Grafana's percent change, (last - first) / first
    over the rows the query returns. That reads 0 % (drawn red) from a single
    row, e.g. when the year before isn't reported or the latest value is
    missing, and flips sign from a negative base. So each query returns
    nothing when the latest value is missing, and otherwise two rows: a
    baseline, then the latest year. The baseline is the year before's value
    when it is positive, else +Infinity, which turns the percent change into
    NaN, and Grafana's stat panel leaves NaN out."""
    stats = [p for p in panels if p["type"] == "stat"]
    assert len(stats) == 6
    for p in stats:
        sql = _sql(p)
        name = p["title"]
        assert "BETWEEN (SELECT fy FROM latest) - 1 AND (SELECT fy FROM latest)" in sql, name
        assert (
            "cur AS (SELECT y.period_end, y.v FROM y"
            " WHERE y.fiscal_year = (SELECT fy FROM latest) AND y.v IS NOT NULL)"
        ) in sql, name
        assert sql.endswith(
            f"CASE WHEN p.v > 0 THEN p.v ELSE 'Infinity'::float8 END AS \"{name}\""
            " FROM cur c LEFT JOIN y p ON p.fiscal_year = (SELECT fy FROM latest) - 1"
            " UNION ALL SELECT c.period_end::timestamptz, c.v FROM cur c ORDER BY 1"
        ), name
        # The baseline sorts before the latest year.
        assert "SELECT COALESCE(p.period_end, c.period_end - 365)::timestamptz" in sql, name
        assert p["options"]["showPercentChange"] is True, name
        assert p["options"]["reduceOptions"]["calcs"] == ["lastNotNull"], name
        assert p["options"]["graphMode"] == "none", name  # the baseline is no data point


def test_statement_tables_latest_year_first_in_millions(panels: list[dict[str, Any]]) -> None:
    tables = {p["title"]: p for p in panels if p["type"] == "table"}
    assert set(tables) == {
        "Income statement — $display_currency millions",
        "Per share — $display_currency",
        "Balance sheet — $display_currency millions",
        "Cash flow statement — $display_currency millions",
    }
    for title, p in tables.items():
        sql = _sql(p)
        assert sql.endswith("ORDER BY m.ord, m.fiscal_year DESC"), title
        if title.endswith("millions"):
            assert "AS value FROM equity_statement_matrix m" in sql
            assert "/ 1e6 AS value" in sql, title
    # Per-share lines live in their own table; share counts are in millions there.
    assert "m.ord < 500" in _sql(tables["Income statement — $display_currency millions"])
    per_share = _sql(tables["Per share — $display_currency"])
    assert "m.ord > 500" in per_share and "m.value / 1e6" in per_share


def test_charts_use_the_validated_palette(panels: list[dict[str, Any]]) -> None:
    charts = [p for p in panels if p["type"] in ("barchart", "trend")]
    assert len(charts) == 6
    for p in charts:
        colors = {
            prop["value"]["fixedColor"]
            for o in p["fieldConfig"]["overrides"]
            for prop in o["properties"]
            if prop["id"] == "color"
        }
        assert colors and colors <= PALETTE, (p["title"], colors)
