"""Migration 0037: the Screener's views, checked on their SQL text.

screener_fundamentals must be built from the views the Valuation dashboard
reads, each computed once; equity_screener must pick each series' point the
way equity_metric_series's windows do, so the Screener's ratios are
Valuation's. Both were also checked against a Postgres database the size of
production's, where every column equalled the Valuation and Watchlist
dashboards' own queries for every company.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "versions"


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{filename}", MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev() -> ModuleType:
    return _load("0037_screener.py")


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_follows_0036(rev: ModuleType) -> None:
    assert (rev.revision, rev.down_revision) == ("0037", "0036")


def test_fundamentals_come_from_valuations_views_each_computed_once(rev: ModuleType) -> None:
    sql = rev.SCREENER_FUNDAMENTALS
    for view in ("equity_flows_ttm", "fundamentals_grouped", "equity_debt"):
        assert len(re.findall(rf"\bFROM {view}\b", sql)) == 1, view
    # Each read is materialized once and shared by the series built from it.
    for cte in ("ttm", "grouped", "book", "debt"):
        assert f"\n{cte} AS MATERIALIZED (" in sql, cte
    assert not re.search(r"\b(FROM|JOIN) (equity_metric_series|fx_usd_daily)\b", sql)


def test_unique_index_allows_concurrent_refresh(rev: ModuleType) -> None:
    # REFRESH ... CONCURRENTLY needs a unique index on plain columns, no WHERE.
    assert _flat(rev.SCREENER_FUNDAMENTALS_INDEX).strip() == (
        "CREATE UNIQUE INDEX ux_screener_fundamentals"
        " ON screener_fundamentals (instrument_id, series, period_end)"
    )


def _series_built(rev: ModuleType) -> set[str]:
    sql = rev.SCREENER_FUNDAMENTALS.split("series(instrument_id, series, period_end, value)")[1]
    return set(re.findall(r"SELECT \w+(?:\.\w+)?, '(\w+)', ", sql))


def test_every_series_the_screener_reads_is_built(rev: ModuleType) -> None:
    read = set(re.findall(r"f\.series = '(\w+)'", rev.EQUITY_SCREENER))
    read |= set(re.findall(r"c\.series = '(\w+)'", rev.EQUITY_SCREENER))
    built = _series_built(rev)
    assert read <= built, read - built
    assert set(rev.MONEY_SERIES) <= built


def test_only_money_carries_the_reporting_currency(rev: ModuleType) -> None:
    sql = _flat(rev.SCREENER_FUNDAMENTALS)
    money = "(" + ", ".join(f"'{s}'" for s in rev.MONEY_SERIES) + ")"
    assert f"CASE WHEN s.series IN {money} THEN r.currency END AS currency" in sql
    # The reporting currency is equity_metric_series's rep_ccy (migration 0031).
    rep_ccy = _flat(_load("0031_reporting_currency_once.py").CCY)
    rep = rep_ccy.split("WITH rep_ccy AS NOT MATERIALIZED (", 1)[1].split("), ccy AS", 1)[0]
    assert rep.strip() in sql


def test_derived_series_follow_equity_metric_series(rev: ModuleType) -> None:
    sql = _flat(rev.SCREENER_FUNDAMENTALS)
    # equity_fcf_ttm and ebitda_w need both of their inputs.
    assert (
        "'fcf', period_end, ocf - capex FROM ttm WHERE ocf IS NOT NULL AND capex IS NOT NULL"
    ) in sql
    assert (
        "'ebitda', period_end, op_income + d_a"
        " FROM ttm WHERE op_income IS NOT NULL AND d_a IS NOT NULL"
    ) in sql
    # 0008's gross margin: gross profit, else revenue less cost of revenue.
    assert "100.0 * COALESCE(gross_profit, revenue - cogs) / NULLIF(revenue, 0)" in sql
    assert "(gross_profit IS NOT NULL OR cogs IS NOT NULL)" in sql
    # shares_w: per period end, the shortest period's weighted average.
    assert "WHERE grp = 'shares_diluted' ORDER BY instrument_id, period_end, period_days ASC" in sql
    # Ratios over book equity or EBITDA only when it is positive.
    assert "CASE WHEN b.value > 0 THEN d.value / b.value END" in sql
    assert "CASE WHEN t.op_income + t.d_a > 0 THEN d.value / (t.op_income + t.d_a) END" in sql
    assert "CASE WHEN b.value > 0 THEN 100.0 * t.net_income / b.value END" in sql


def test_points_are_read_through_equity_metric_series_windows(rev: ModuleType) -> None:
    # 0002/0008: a price joins the fundamentals row whose period it falls in
    # (period_end <= day < next period_end) while day < period_end + 450.
    window = _load("0012_price_metric.py")._BASE_BRANCHES
    assert f"p.date < (e.period_end + {rev.STALE_DAYS})" in window
    latest = _flat(rev._latest("eps", "eps"))
    assert "f.period_end <= p.ref_date ORDER BY f.period_end DESC LIMIT 1" in latest
    assert latest.endswith(f") eps ON eps.period_end > p.ref_date - {rev.STALE_DAYS}")


def test_ratios_are_priced_like_equity_metric_series(rev: ModuleType) -> None:
    sql = _flat(rev.EQUITY_SCREENER)
    # 0028's px_rep: the close at its own day's rate, unconverted when the
    # currencies match or a rate is missing.
    for alias in ("eps", "fcf", "bk", "eb"):
        assert (
            f"p.close * CASE WHEN {alias}.currency IS NULL OR {alias}.currency = p.currency THEN 1"
            f" ELSE COALESCE(fx_usd_rate(p.currency, p.price_date)"
            f" / NULLIF(fx_usd_rate({alias}.currency, p.price_date), 0), 1) END"
        ) in sql, alias
    assert "CASE WHEN eps.value > 0 THEN" in sql
    assert "CASE WHEN fcf.value > 0" in sql
    assert "CASE WHEN bk.value > 0" in sql
    assert "CASE WHEN eb.value > 0" in sql
    assert "+ COALESCE(dt.value, 0) - COALESCE(ca.value, 0)) / eb.value" in sql
    # Market cap stays in the listing currency, like the view's mcap.
    assert "p.close * sh.value AS mcap" in sql


def test_growth_pairs_points_like_valuations_growth_chart(rev: ModuleType) -> None:
    valuation = json.loads((ROOT / "grafana" / "dashboards" / "valuation.json").read_text())
    (chart,) = [
        p for p in valuation["panels"]
        if p.get("title", "").startswith("Growth (TTM, year on year)")
    ]
    chart_sql = chart["targets"][0]["rawSql"]
    assert (
        "BETWEEN c.\"time\" - interval '395 days' AND c.\"time\" - interval '335 days'"
    ) in chart_sql
    assert "CASE WHEN p.v > 0 THEN 100.0 * (c.v - p.v) / p.v END" in chart_sql
    growth = _flat(rev._growth("revenue", "rg"))
    assert "f.period_end BETWEEN c.period_end - 395 AND c.period_end - 335" in growth
    assert "ORDER BY abs(f.period_end - (c.period_end - 365)), f.period_end LIMIT 1" in growth
    assert "CASE WHEN b.value > 0 THEN 100.0 * (c.value - b.value) / b.value END" in growth
    # The latest point that has a year-earlier one (the chart's inner join).
    assert ") b ON true WHERE c.instrument_id = p.instrument_id" in growth


def test_price_moves_follow_the_watchlist(rev: ModuleType) -> None:
    sql = _flat(rev.EQUITY_SCREENER)
    for condition in (
        "x.date <= p.price_date - 30",
        "x.date < date_trunc('year', p.price_date)::date",
        "x.date <= p.price_date - 365",
        "x.date > p.price_date - 365 AND x.date <= p.price_date",
    ):
        assert condition in sql, condition
    assert "CASE WHEN count(*) = 200 THEN avg(s.close) END" in sql
    # Once per company: a LATERAL, not a CTE joined back (which the planner
    # re-ran for every row: 1.3 s instead of 35 ms).
    assert "LEFT JOIN LATERAL ( SELECT (SELECT x.close::float8 FROM prices x" in sql
    assert "moves AS" not in sql


def test_reads_every_equity_and_its_lists(rev: ModuleType) -> None:
    sql = _flat(rev.EQUITY_SCREENER)
    assert "FROM instruments i WHERE i.kind = 'equity'" in sql
    assert "COALESCE(i.sector, 'Unclassified') AS sector" in sql
    assert "EXISTS (SELECT 1 FROM portfolio_held_instruments h WHERE h.instrument_id = i.id)" in sql
    assert "ed.earnings_date >= CURRENT_DATE" in sql
    assert not re.search(r"\b(FROM|JOIN) (equity_metric_series|fx_usd_daily)\b", sql)


def test_downgrade_drops_both(rev: ModuleType) -> None:
    calls: list[str] = []
    rev.op = type("Op", (), {"execute": staticmethod(calls.append)})
    rev.downgrade()
    assert calls == ["DROP VIEW equity_screener", "DROP MATERIALIZED VIEW screener_fundamentals"]
    calls.clear()
    rev.upgrade()
    assert [c.split(" AS", 1)[0].strip() for c in calls] == [
        "CREATE MATERIALIZED VIEW screener_fundamentals",
        rev.SCREENER_FUNDAMENTALS_INDEX.strip(),
        "CREATE VIEW equity_screener",
    ]
