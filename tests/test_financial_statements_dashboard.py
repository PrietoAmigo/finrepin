"""The Financial Statements dashboard's layout rules, checked on its JSON.

Every panel reads fiscal years or fiscal quarters, as the Period picker says.
Highlights compare the latest selected period with a year earlier; the
statement tables put the latest period first and show money in millions (a
fixed scale, so a row never switches between Mil and Bil); charts use only
the validated categorical palette.
"""

from __future__ import annotations

import json
import re
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


def _variables(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in dashboard["templating"]["list"]}


def test_sections_in_reading_order(panels: list[dict[str, Any]]) -> None:
    rows = [p["title"] for p in panels if p["type"] == "row"]
    assert rows == [
        "$heading",
        "Trends",
        "Income statement",
        "Balance sheet",
        "Cash flow statement",
    ]


def test_period_picker_defaults_to_quarterly(dashboard: dict[str, Any]) -> None:
    variables = _variables(dashboard)
    assert list(variables) == [
        "ticker",
        "freq",
        "fiscal_year",
        "quarters",
        "display_currency",
        "since",
        "heading",
    ]
    freq = variables["freq"]
    assert freq["type"] == "custom" and freq["label"] == "Period"
    assert freq["query"] == "Annual : annual,Quarterly : quarterly"
    assert freq["current"]["value"] == "quarterly"
    assert [o["value"] for o in freq["options"] if o["selected"]] == ["quarterly"]
    assert [o["value"] for o in freq["options"]] == ["annual", "quarterly"]


def test_quarterly_view_shows_the_latest_eight_quarters_by_default(
    dashboard: dict[str, Any],
) -> None:
    variables = _variables(dashboard)
    quarters = variables["quarters"]
    assert quarters["current"]["value"] == "8"
    assert [o["value"] for o in quarters["options"] if o["selected"]] == ["8"]
    # All is no LIMIT: the hidden `since` variable casts it to LIMIT NULL.
    assert quarters["includeAll"] is True and quarters["allValue"] == "NULL"
    since = variables["since"]
    assert since["hide"] == 2
    assert "LIMIT CASE WHEN '$freq' = 'quarterly' THEN ($quarters)::int END" in since["query"]
    # Counted per period label, so a fiscal year whose lines end on two
    # different days still counts once.
    assert "GROUP BY p.period ORDER BY 1 DESC" in since["query"]
    assert "COALESCE(min(s.period_end), DATE '1900-01-01')" in since["query"]


def test_fiscal_years_list_both_frequencies(dashboard: dict[str, Any]) -> None:
    # Not filtered on $freq: switching Period keeps the selected years, and a
    # company with no quarters still has years to pick (an empty list would
    # interpolate as `IN ()`, a syntax error in every panel).
    query = _variables(dashboard)["fiscal_year"]["query"]
    assert "FROM equity_statement_periods a" in query and "$freq" not in query


def test_heading_names_the_period_or_why_there_are_no_quarters(
    dashboard: dict[str, Any],
) -> None:
    heading = _variables(dashboard)["heading"]
    assert heading["hide"] == 2
    query = heading["query"]
    assert "'Highlights — FY' || p.period || ' vs a year earlier, $display_currency'" in query
    assert "ORDER BY p.period_end DESC LIMIT 1" in query
    assert "'No quarterly figures for $ticker: '" in query
    assert "annual reports only (Form 20-F / 40-F)" in query
    assert "it reports half-yearly, not quarterly" in query


def test_every_panel_reads_the_selected_frequency(panels: list[dict[str, Any]]) -> None:
    for p in panels:
        if p["type"] == "row":
            continue
        sql = _sql(p)
        assert "equity_statement_annual" not in sql and "equity_statement_matrix" not in sql
        assert re.search(r"\b(\w)\.freq = '\$freq'", sql), p["title"]
        # Trends and tables show the periods the tables do: since `since`.
        if p["type"] != "stat":
            assert "period_end >= '$since'" in sql, p["title"]


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
        # A quarter is compared with the same quarter of the fiscal year
        # before; a fiscal year (no quarter) with the year before.
        assert "ORDER BY l.period_end DESC LIMIT 1), " in sql, name
        assert (
            "AND COALESCE(a.fiscal_quarter, 0) = COALESCE((SELECT fq FROM latest), 0)"
        ) in sql, name
        assert p["options"]["showPercentChange"] is True, name
        assert p["options"]["reduceOptions"]["calcs"] == ["lastNotNull"], name
        assert p["options"]["graphMode"] == "none", name  # the baseline is no data point


def test_statement_tables_latest_period_first_in_millions(panels: list[dict[str, Any]]) -> None:
    tables = {p["title"]: p for p in panels if p["type"] == "table"}
    assert set(tables) == {
        "Income statement — $display_currency millions",
        "Per share — $display_currency",
        "Balance sheet — $display_currency millions",
        "Cash flow statement — $display_currency millions",
    }
    for title, p in tables.items():
        sql = _sql(p)
        assert sql.endswith("ORDER BY m.ord, m.period_end DESC"), title
        assert "m.period AS period" in sql, title
        assert "FROM equity_statement_period_matrix m" in sql, title
        if title.endswith("millions"):
            assert "/ 1e6 AS value" in sql, title
        grouping = next(t for t in p["transformations"] if t["id"] == "groupingToMatrix")
        assert grouping["options"] == {
            "columnField": "period",
            "rowField": "line",
            "valueField": "value",
        }
    # Per-share lines live in their own table; share counts are in millions there.
    assert "m.ord < 500" in _sql(tables["Income statement — $display_currency millions"])
    per_share = _sql(tables["Per share — $display_currency"])
    assert "m.ord > 500" in per_share and "m.value / 1e6" in per_share


def test_trend_charts_are_bars_over_period_labels(panels: list[dict[str, Any]]) -> None:
    # A trend panel needs a numeric x axis, which can't label "2025 Q3".
    charts = [p for p in panels if p["type"] in ("barchart", "trend", "timeseries")]
    assert {p["type"] for p in charts} == {"barchart"}
    for p in charts:
        assert p["options"]["xField"] == "Period", p["title"]
        sql = _sql(p)
        assert 'SELECT period AS "Period", ' in sql, p["title"]
        assert sql.endswith("FROM a GROUP BY period ORDER BY max(period_end)"), p["title"]


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
