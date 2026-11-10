"""The Screener dashboard's rules, checked on its JSON.

Every panel reads equity_screener through the same selectors; the table's
Sort by selector covers each option with blanks last; the Columns selector
only names columns the table's query returns, always with the identifying
ones; and a symbol opens the company's Valuation and Financial Statements.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
DASHBOARD = DASHBOARDS / "screener.json"
ALWAYS = ["Symbol", "Name", "Sector", "Mkt cap"]


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


@pytest.fixture(scope="module")
def variables(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in dashboard["templating"]["list"]}


@pytest.fixture(scope="module")
def table(panels: list[dict[str, Any]]) -> dict[str, Any]:
    (t,) = [p for p in panels if p["title"] == "Stocks" and p["type"] == "table"]
    return t


def _sql(panel: dict[str, Any]) -> str:
    return str(panel["targets"][0]["rawSql"])


def _aliases(sql: str) -> list[str]:
    select = sql.rsplit("FROM picked s", 1)[0].split(") SELECT ", 1)[1]
    return re.findall(r' AS "([^"]+)"', select)


def _options(var: dict[str, Any]) -> list[tuple[str, str]]:
    """A custom variable's (label, value) pairs, parsed as Grafana parses them."""
    pairs = []
    for item in re.findall(r"(?:\\,|[^,])+", var["query"]):
        label, value = re.match(r"^(.+)\s:\s(.+)$", item.replace("\\,", ",")).groups()  # type: ignore[union-attr]
        pairs.append((label.strip(), value.strip()))
    return pairs


def test_identity(dashboard: dict[str, Any]) -> None:
    assert (dashboard["uid"], dashboard["title"]) == ("fintracker-screener", "Screener")
    assert dashboard["tags"] == ["fintracker"]
    # Nothing on it depends on the time range.
    assert dashboard["timepicker"]["hidden"] is True
    for p in _panels(dashboard["panels"]):
        assert "$__time" not in _sql(p), p["title"]


def test_every_panel_reads_the_screener_through_the_same_selectors(
    panels: list[dict[str, Any]],
) -> None:
    sqls = [_sql(p) for p in panels]
    assert len(sqls) == 8
    base = sqls[0].split(") SELECT ", 1)[0]
    for p, sql in zip(panels, sqls, strict=True):
        assert sql.startswith(base), p["title"]
        assert sql.count("FROM equity_screener e") == 1, p["title"]
        assert "FROM picked s" in sql, p["title"]
    for clause in (
        "e.sector IN ($sector)",
        "e.region IN ($region)",
        "('$universe' = 'held' AND e.held)",
        "('$universe' = 'watchlist' AND e.watched)",
        "('$universe' = 'other' AND NOT e.held AND NOT e.watched)",
        "WHERE $min_mcap = 0 OR s.mcap_ccy >= $min_mcap",
    ):
        assert clause in base, clause


def test_market_cap_is_converted_or_blank(panels: list[dict[str, Any]]) -> None:
    base = _sql(panels[0])
    # At the close's own rate; with no rate stored it is blank (no COALESCE
    # back to the unconverted figure, which would sort and filter as if it were
    # in the selected currency).
    assert (
        "e.mcap * CASE WHEN e.currency = '$display_currency' THEN 1"
        " ELSE fx_usd_rate(e.currency, e.price_date)"
        " / NULLIF(fx_usd_rate('$display_currency', e.price_date), 0) END AS mcap_ccy"
    ) in base


def test_universe_options_match_the_query(variables: dict[str, dict[str, Any]],
                                          panels: list[dict[str, Any]]) -> None:
    assert [v for _, v in _options(variables["universe"])] == ["all", "held", "watchlist", "other"]
    for _, value in _options(variables["universe"]):
        assert f"'$universe' = '{value}'" in _sql(panels[0]), value


def test_min_market_cap_options_are_numbers(variables: dict[str, dict[str, Any]]) -> None:
    values = [v for _, v in _options(variables["min_mcap"])]
    assert values[0] == "0"
    assert [int(v) for v in values] == sorted(int(v) for v in values)


def test_selectors_list_what_the_view_holds(variables: dict[str, dict[str, Any]]) -> None:
    for name in ("sector", "region"):
        var = variables[name]
        assert var["multi"] is True and var["includeAll"] is True, name
        assert f"COALESCE({name}, 'Unclassified')" in var["query"], name
        assert "kind = 'equity'" in var["query"], name
    # Only currencies with a rate to convert into.
    assert "symbol LIKE '%/USD'" in variables["display_currency"]["query"]


def test_custom_selectors_take_only_their_options(variables: dict[str, dict[str, Any]]) -> None:
    # Their values are pasted into SQL ($min_mcap as a number).
    for var in variables.values():
        if var["type"] == "custom":
            assert var["allowCustomValue"] is False, var["name"]
            assert [o["value"] for o in var["options"] if o["value"] != "$__all"] == [
                v for _, v in _options(var)
            ], var["name"]


def test_sort_by_covers_every_option_blanks_last(variables: dict[str, dict[str, Any]],
                                                  table: dict[str, Any]) -> None:
    order = _sql(table).rsplit("ORDER BY", 1)[1]
    for label, value in _options(variables["sort"]):
        if value == "symbol":
            continue
        assert f"'{value}'" in order, label
        expected = "ASC" if "lowest" in label or "soonest" in label else "DESC"
        clause = next(c for c in re.split(r"(?<=NULLS LAST),", order) if f"'{value}'" in c)
        assert clause.strip().endswith(f"{expected} NULLS LAST"), label
    assert order.strip().endswith("s.symbol")
    assert variables["sort"]["current"]["value"] == "mcap"
    # The SQL order is the table's: no default header sort overrides it.
    assert "sortBy" not in table["options"]


def test_columns_selector_names_only_columns_the_table_returns(
    variables: dict[str, dict[str, Any]], table: dict[str, Any]
) -> None:
    aliases = _aliases(_sql(table))
    assert set(ALWAYS) <= set(aliases)
    groups = _options(variables["columns"])
    assert [label for label, _ in groups] == [
        "Overview", "Valuation", "Growth", "Profitability", "Debt", "Price", "Details",
    ]
    shown: set[str] = set()
    for label, value in groups:
        names = value.split(",")
        assert names[:4] == ALWAYS, label
        assert set(names) <= set(aliases), (label, set(names) - set(aliases))
        shown |= set(names)
    # Every column is in some group, so "All" is the whole table.
    assert shown == set(aliases)
    assert variables["columns"]["includeAll"] is True
    assert variables["columns"]["current"]["text"] == ["Overview"]
    assert table["transformations"] == [{
        "id": "filterFieldsByName",
        "options": {"byVariable": True, "include": {"variable": "$columns"}},
    }]


def test_table_overrides_name_real_columns(table: dict[str, Any]) -> None:
    aliases = set(_aliases(_sql(table)))
    names = [o["matcher"]["options"] for o in table["fieldConfig"]["overrides"]]
    for o in table["fieldConfig"]["overrides"]:
        assert o["matcher"]["id"] == "byName"
    assert set(names) == aliases, set(names) ^ aliases
    assert len(names) == len(set(names))
    # Filterable headers, and a row count that follows their filters.
    assert table["fieldConfig"]["defaults"]["custom"]["filterable"] is True
    assert table["options"]["footer"]["countRows"] is True


def test_market_cap_header_names_the_currency(panels: list[dict[str, Any]]) -> None:
    # The query's field name is fixed (the Columns selector matches it); the
    # header shows the currency.
    for p in panels:
        if p["type"] != "table":
            continue
        assert '"Mkt cap"' in _sql(p) and "($display_currency)" not in _sql(p), p["title"]
        (mcap,) = [o for o in p["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Mkt cap"]
        props = {x["id"]: x["value"] for x in mcap["properties"]}
        assert props["displayName"] == "Mkt cap ($display_currency)", p["title"]
        assert props["unit"] == "short", p["title"]


def test_symbols_open_valuation_and_statements(table: dict[str, Any]) -> None:
    overrides = table["fieldConfig"]["overrides"]
    (symbol,) = [o for o in overrides if o["matcher"]["options"] == "Symbol"]
    links = {x["id"]: x["value"] for x in symbol["properties"]}["links"]
    assert [link["url"] for link in links] == [
        "/d/fintracker-ticker-fundamentals/valuation"
        "?var-ticker=${__value.raw}&var-display_currency=${display_currency}",
        "/d/fintracker-financial-statements/financial-statements"
        "?var-ticker=${__value.raw}&var-display_currency=${display_currency}",
    ]


def test_summary_tiles_are_medians_of_the_picked_stocks(panels: list[dict[str, Any]]) -> None:
    tiles = [p for p in panels if p["type"] == "stat"]
    assert [p["title"] for p in tiles] == [
        "Stocks", "Median P/E", "Median EV/EBITDA", "Median rev growth", "Median op margin",
        "Median 1Y return",
    ]
    assert 'SELECT count(*) AS "Stocks" FROM picked s' in _sql(tiles[0])
    for p in tiles[1:]:
        assert "percentile_cont(0.5) WITHIN GROUP" in _sql(p), p["title"]
    assert [(p["gridPos"]["x"], p["gridPos"]["w"]) for p in tiles] == [
        (x, 4) for x in range(0, 24, 4)
    ]


def test_sectors_table(panels: list[dict[str, Any]]) -> None:
    (p,) = [p for p in panels if p["title"] == "Sectors"]
    sql = _sql(p)
    assert "GROUP BY s.sector" in sql and 'count(*) AS "Stocks"' in sql
    aliases = set(re.findall(r' AS "([^"]+)"', sql.split(") SELECT ", 1)[1]))
    names = {o["matcher"]["options"] for o in p["fieldConfig"]["overrides"]}
    assert names <= aliases, names - aliases


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


def test_listed_with_the_other_dashboards() -> None:
    uids = {json.loads(p.read_text())["uid"] for p in DASHBOARDS.glob("*.json")}
    assert "fintracker-screener" in uids
    assert len(uids) == len(list(DASHBOARDS.glob("*.json")))
