"""The Valuation dashboard's layout rules, checked on its JSON.

It was "Ticker Fundamentals", then "Fundamentals": the title changed, the uid
did not, so bookmarks and links keep working. A snapshot of stat tiles leads; the valuation charts
show each ratio against its own median and middle half; charts use only the
validated categorical palette plus neutral context ink.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
DASHBOARD = DASHBOARDS / "valuation.json"
UID = "fintracker-ticker-fundamentals"
# Validated categorical slots (blue, orange, aqua) on Grafana's dark panel
# surface, the same as Financial Statements, and the neutral inks used for
# context (medians, bands, the 200-period average, the 0% line).
PALETTE = {"#3987e5", "#d95926", "#199e70"}
NEUTRAL = {"#c3c2b7", "#898781", "text", "transparent"}


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


def test_renamed_but_same_uid(dashboard: dict[str, Any]) -> None:
    assert dashboard["title"] == "Valuation"
    assert dashboard["uid"] == UID
    for old in ("ticker-fundamentals.json", "fundamentals.json"):
        assert not (DASHBOARDS / old).exists()


def test_links_use_the_new_slug() -> None:
    urls = []
    for path in DASHBOARDS.glob("*.json"):
        text = path.read_text()
        assert "/ticker-fundamentals" not in text, path.name
        urls += re.findall(rf"/d/{UID}/[\w-]+", text)
    assert urls and set(urls) == {f"/d/{UID}/valuation"}


def test_linked_both_ways_with_financial_statements(dashboard: dict[str, Any]) -> None:
    statements = json.loads((DASHBOARDS / "financial-statements.json").read_text())
    for source, target in ((dashboard, "financial-statements"), (statements, "valuation")):
        (link,) = source["links"]
        assert f"/{target}?var-ticker=${{ticker}}&var-display_currency=${{display_currency}}" in (
            link["url"]
        )


def test_sections_in_reading_order(panels: list[dict[str, Any]]) -> None:
    rows = [p["title"] for p in panels if p["type"] == "row"]
    assert rows == [
        "$company_name ($ticker) · $display_currency · $next_earnings",
        "Price",
        "Valuation against its own history",
        "Growth and profitability",
        "Leverage and shares",
    ]


def test_leverage_row(panels: list[dict[str, Any]]) -> None:
    row = next(p for p in panels if p["title"] == "Leverage and shares")
    below = sorted(
        (p for p in panels if p["gridPos"]["y"] == row["gridPos"]["y"] + 1),
        key=lambda p: p["gridPos"]["x"],
    )
    assert [(p["title"], p["gridPos"]["w"]) for p in below] == [
        ("Debt-to-equity", 8),
        ("Debt to earnings (TTM)", 8),
        ("Shares outstanding", 8),
    ]
    assert not any(p["title"].startswith("Total debt") for p in panels)
    sql = _sql(below[1])
    # One company's TTM flows (pushed into the view), and no ratio over
    # earnings that are zero or negative.
    assert re.search(
        r"FROM equity_flows_ttm t WHERE t\.instrument_id = "
        r"\(SELECT id FROM instruments WHERE symbol = '\$ticker'\)",
        sql,
    )
    assert "CASE WHEN f.ebitda > 0" in sql and "CASE WHEN f.net_income > 0" in sql


def test_growth_and_leverage_legends_show_the_last_value(panels: list[dict[str, Any]]) -> None:
    start = next(p for p in panels if p["title"] == "Growth and profitability")["gridPos"]["y"]
    charts = [p for p in panels if p["type"] == "timeseries" and p["gridPos"]["y"] > start]
    assert len(charts) == 8
    for p in charts:
        legend = p["options"]["legend"]
        assert legend["showLegend"] is True, p["title"]
        assert legend["calcs"] == ["lastNotNull"], p["title"]
        assert legend["displayMode"] == "table", p["title"]


def test_header_variables_are_hidden(dashboard: dict[str, Any]) -> None:
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    for name in ("company_name", "next_earnings"):
        assert variables[name]["hide"] == 2 and variables[name]["skipUrlSync"] is True
    # Always one row, so the header never reads "$next_earnings".
    assert variables["next_earnings"]["query"].startswith("SELECT COALESCE(")


def test_layout_has_no_overlaps(panels: list[dict[str, Any]]) -> None:
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    cells: dict[tuple[int, int], int] = {}
    for p in panels:
        g = p["gridPos"]
        assert g["x"] >= 0 and g["x"] + g["w"] <= 24, p["title"]
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, (p["id"], cells.get((x, y)))
                cells[(x, y)] = p["id"]


def test_snapshot_tiles(panels: list[dict[str, Any]]) -> None:
    tiles = [p for p in panels if p["type"] == "stat"]
    assert [p["title"] for p in tiles] == [
        "Price · 1y",
        "Market cap · 1y",
        "P/E (TTM) · vs median",
        "P/FCF (TTM) · vs median",
        "EV/EBITDA (TTM) · vs median",
    ]
    # One row, left to right, filling the 24 columns; the price, with the
    # shortest title, is the narrow one.
    widths = [(p["gridPos"]["x"], p["gridPos"]["w"]) for p in tiles]
    assert widths == [(0, 4), (4, 5), (9, 5), (14, 5), (19, 5)]
    for p in tiles:
        # Half height: one line of title over one line of figures.
        assert (p["gridPos"]["h"], p["gridPos"]["y"]) == (2, 1), p["title"]
        assert p["options"]["showPercentChange"] is True, p["title"]
        assert p["options"]["reduceOptions"]["calcs"] == ["lastNotNull"], p["title"]
        assert p["options"]["graphMode"] == "none", p["title"]  # numbers only, no sparkline
        assert (p.get("timeFrom") == "1y") is p["title"].endswith("· 1y"), p["title"]
        if p["title"].endswith("vs median"):
            # Richer than usual reads red. No rows unless the last day has a
            # ratio, so the tile never shows a stale one.
            assert p["options"]["percentChangeColorMode"] == "inverted", p["title"]
            sql = _sql(p)
            assert "percentile_cont(0.5)" in sql and "l.value IS NOT NULL" in sql, p["title"]
        else:
            assert p["options"]["percentChangeColorMode"] == "standard", p["title"]


def test_market_cap_tile(panels: list[dict[str, Any]]) -> None:
    tile = next(p for p in panels if p["title"] == "Market cap · 1y")
    sql = _sql(tile)
    # The view's daily close x diluted shares, converted day by day into the
    # display currency like the price.
    assert "s.metric = 'mcap'" in sql
    assert 'fx_usd_rate(s.currency, s."time"::date)' in sql
    assert "fx_usd_rate('$display_currency', s.\"time\"::date)" in sql
    # No rows once the series has stopped (shares older than ~15 months), so
    # the tile never shows a stale market cap as the current one.
    assert "> $__timeTo()::timestamptz - interval '31 days'" in sql
    assert tile["fieldConfig"]["defaults"]["unit"] == "short"  # 2.5 Tri, 340 Bil


def test_valuation_charts_shade_the_middle_half(panels: list[dict[str, Any]]) -> None:
    charts = [p for p in panels if p.get("targets") and "ARRAY[0.25, 0.5, 0.75]" in _sql(p)]
    assert [p["title"] for p in charts] == ["P/E (TTM)", "P/FCF (TTM)", "P/B", "EV/EBITDA (TTM)"]
    for p in charts:
        overrides = {
            o["matcher"]["options"]: {prop["id"]: prop["value"] for prop in o["properties"]}
            for o in p["fieldConfig"]["overrides"]
        }
        assert overrides["75th percentile"]["custom.fillBelowTo"] == "25th percentile"
        assert overrides["Median"]["custom.lineStyle"]["fill"] == "dash"


def test_price_panel_adds_volume_and_report_markers(
    dashboard: dict[str, Any], panels: list[dict[str, Any]]
) -> None:
    price = next(p for p in panels if p["type"] == "candlestick")
    assert price["options"]["mode"] == "candles+volume"
    assert "sum(v) AS v" in _sql(price) and 'v AS "volume"' in _sql(price)
    reports = next(a for a in dashboard["annotations"]["list"] if a["name"] == "Earnings reports")
    assert reports["filter"] == {"exclude": False, "ids": [price["id"]]}
    assert reports["enable"] is False  # opt-in: ~40 markers over ten years


def _scale_key(config: dict[str, Any]) -> tuple[Any, ...]:
    """What Grafana's buildScaleKey (11.6) reads: fields with equal keys share a y-scale."""
    custom = config.get("custom", {})
    return (
        config.get("unit"),
        config.get("min"),
        config.get("max"),
        custom.get("axisSoftMin"),
        custom.get("axisSoftMax"),
        custom.get("axisPlacement", "auto"),
        json.dumps(custom.get("scaleDistribution"), sort_keys=True),
        custom.get("axisLabel") or None,
    )


def test_volume_gets_its_own_y_scale(panels: list[dict[str, Any]]) -> None:
    # The candlestick panel sets volume's unit to 'short' and ranges its scale
    # to [0, 7 x max]. On the prices' scale it stretches the price axis to the
    # volume, which flattens the candles out of sight.
    price = next(p for p in panels if p["type"] == "candlestick")
    defaults = price["fieldConfig"]["defaults"]

    def config(field: str) -> dict[str, Any]:
        merged = json.loads(json.dumps(defaults))
        for o in price["fieldConfig"]["overrides"]:
            if o["matcher"] == {"id": "byName", "options": field}:
                for prop in o["properties"]:
                    *path, leaf = prop["id"].split(".")
                    node = merged
                    for part in path:
                        node = node.setdefault(part, {})
                    node[leaf] = prop["value"]
        return merged

    volume = {**config("volume"), "unit": "short"}
    for field in ("open", "high", "low", "close", "SMA50", "SMA200"):
        assert _scale_key(config(field)) != _scale_key(volume), field


def _colors(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("color", "fixedColor", "up", "down", "flat") and isinstance(value, str):
                yield value
            else:
                yield from _colors(value)
    elif isinstance(node, list):
        for item in node:
            yield from _colors(item)


def test_colors_come_from_the_validated_palette(panels: list[dict[str, Any]]) -> None:
    used = {c for p in panels for c in _colors(p)}
    assert used >= PALETTE
    assert used <= PALETTE | NEUTRAL, used - PALETTE - NEUTRAL
