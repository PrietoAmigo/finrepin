"""Migration 0036: TTM quarters from year-to-date facts, checked on the SQL text.

The quarters behind every TTM figure must use fundamentals_grouped's tags
and priorities (so a TTM sum and the filed full year read the same tag), a
year-to-date difference must stay within one tag, unit and start, and an
instrument filter must still reach the view: the only CTE read twice is
inlined. The SQL's results were checked against a Postgres database with
SEC-shaped data, where TTM operating cash flow, capex and D&A went from a
point per fiscal year to one per quarter, each equal to the true sum of the
last four quarters.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
FAMILY_ROW = re.compile(r"\('(\w+)', '(\w+)', (\d+)\)")


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{filename}", MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0036() -> ModuleType:
    return _load("0036_ttm_from_year_to_date.py")


def test_families_are_fundamentals_groupeds_flow_families(rev0036: ModuleType) -> None:
    rev0034 = _load("0034_legacy_xbrl_tags.py")
    grouped = FAMILY_ROW.findall(rev0034.families_0008() + "," + rev0034.LEGACY_FAMILIES)
    flow = {"revenue", "eps", "op_income", "ocf", "capex", "net_income", "gross_profit",
            "cogs", "d_a"}
    expected = [(g, t, int(p)) for g, t, p in grouped if g in flow]
    assert rev0036.flow_families() == expected
    assert {g for g, _, _ in expected} == flow
    sql = rev0036.flows_quarterly_sql()
    values = sql.split("families(grp, tag, priority) AS (VALUES", 1)[1].split("\n),", 1)[0]
    assert [(g, t, int(p)) for g, t, p in FAMILY_ROW.findall(values)] == expected


def test_a_year_to_date_difference_stays_within_one_tag_unit_and_start(
    rev0036: ModuleType,
) -> None:
    sql = rev0036.flows_quarterly_sql()
    window = re.sub(r"\s+", " ", sql.split("WINDOW w AS (", 1)[1].split(")", 1)[0])
    assert window == (
        "PARTITION BY f.instrument_id, m.grp, m.priority, f.unit, f.period_start"
        " ORDER BY f.period_end"
    )
    assert "OR (period_end - prev_end) BETWEEN 60 AND 120" in sql
    assert "ELSE value - prev_value END AS value" in sql


def test_one_quarter_per_end_tag_priority_first_fallback_last(rev0036: ModuleType) -> None:
    sql = rev0036.flows_quarterly_sql()
    assert "SELECT DISTINCT ON (instrument_id, grp, period_end)" in sql
    assert re.sub(r"\s+", " ", sql.rsplit("ORDER BY", 1)[1]).strip() == (
        "instrument_id, grp, period_end, priority, derived,"
        " CASE WHEN unit IN ('USD', 'USD/shares', 'shares') THEN 0 ELSE 1 END"
    )
    # 0008's full-year-less-three-quarters Q4 sorts after every tag's own.
    priorities = [p for _, _, p in rev0036.flow_families()]
    assert max(priorities) < rev0036.FALLBACK_PRIORITY
    assert f"{rev0036.FALLBACK_PRIORITY}, 2, NULL\n    FROM derived_q4" in sql


def test_output_columns_are_unchanged(rev0036: ModuleType) -> None:
    # CREATE OR REPLACE VIEW needs the old columns, in order.
    sql = rev0036.flows_quarterly_sql()
    final = sql.rsplit("SELECT DISTINCT ON (instrument_id, grp, period_end)", 1)[1]
    assert final.split("FROM", 1)[0].split() == ["instrument_id,", "grp,", "period_end,", "value"]
    assert sql.lstrip().startswith("CREATE OR REPLACE VIEW equity_flows_quarterly AS")


def test_only_the_cte_read_twice_is_inlined_explicitly(rev0036: ModuleType) -> None:
    sql = rev0036.flows_quarterly_sql()
    names = re.findall(r"^(?:WITH )?(\w+)(?:\([^)]*\))? AS (NOT MATERIALIZED )?\(", sql, re.M)
    assert [n for n, _ in names] == [
        "families", "facts", "own_quarters", "quarterly", "annual", "derived_q4"
    ]
    for name, inlined in names:
        reads = len(re.findall(rf"\b(?:FROM|JOIN) {name}\b", sql))
        assert reads == (2 if inlined else 1), (name, reads)
    assert dict(names)["quarterly"] == "NOT MATERIALIZED "


def test_downgrade_restores_0029s_view(rev0036: ModuleType) -> None:
    source = (MIGRATIONS / "0036_ttm_from_year_to_date.py").read_text()
    downgrade = source.split("def downgrade()", 1)[1]
    assert "0029_metric_series_per_instrument.py" in downgrade
    assert "flows_quarterly_sql()" in downgrade
