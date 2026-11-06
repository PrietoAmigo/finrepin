"""Migration 0035: the quarterly statement views, checked on their SQL text.

Quarterly lines must be the annual view's lines (the dashboard shows either
under the same tables), a derived quarter must stay within one tag, unit and
start, and an instrument filter must reach every part of the views: no CTE
may be read twice, which would let Postgres materialize it for every
instrument. The SQL's results were checked against a Postgres database with
SEC-shaped data (reported and year-to-date facts, a 52/53-week fiscal year);
these checks keep the text from drifting without one.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MAPPING_ROW = re.compile(r"\('(\w+)', (\d+), '([^']*)', (\d+),\s*'([^']*)', (\d+),\s*'(\w+)'\)")


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{filename}", MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0035() -> ModuleType:
    return _load("0035_quarterly_statements.py")


def _rows(text: str) -> list[tuple[str, int, str, int, str, int, str]]:
    return [
        (st, int(so), sec, int(io), line, int(pr), tag)
        for st, so, sec, io, line, pr, tag in MAPPING_ROW.findall(text)
    ]


def _cte_names(sql: str) -> list[str]:
    return re.findall(r"^(?:WITH )?(\w+)(?:\([^)]*\))? AS (?:NOT MATERIALIZED )?\(", sql, re.M)


def test_quarterly_lines_are_the_annual_views(rev0035: ModuleType) -> None:
    rev0007 = _load("0007_currency_views.py")
    rev0034 = _load("0034_legacy_xbrl_tags.py")
    annual = _rows(str(rev0007.STATEMENT_MAPPING) + str(rev0034.LEGACY_MAPPING))
    assert len(annual) > 150
    assert rev0035.statement_mapping() == annual
    sql = rev0035.quarterly_view_sql()
    flows, balance = sql.split("balance_mapping", 1)
    assert sorted(_rows(flows) + _rows(balance.split("facts AS", 1)[0])) == sorted(annual)
    assert {r[0] for r in _rows(flows)} == {"income", "cashflow"}
    assert {r[0] for r in _rows(balance.split("facts AS", 1)[0])} == {"balance"}


def test_every_cte_is_read_once(rev0035: ModuleType) -> None:
    for sql in (rev0035.quarterly_view_sql(), rev0035.PERIOD_MATRIX_VIEW):
        names = _cte_names(sql)
        assert names, sql[:80]
        for name in names:
            reads = re.findall(rf"\b(?:FROM|JOIN) {name}\b", sql)
            assert len(reads) == 1, f"{name} is read {len(reads)} times"


def test_a_derived_quarter_stays_within_one_tag_unit_and_start(rev0035: ModuleType) -> None:
    sql = rev0035.quarterly_view_sql()
    window = sql.split("WINDOW w AS (", 1)[1].split(")", 1)[0]
    partition = re.sub(r"\s+", " ", window)
    assert partition == (
        "PARTITION BY f.instrument_id, m.statement, m.line, m.priority, f.unit, f.period_start"
        " ORDER BY f.period_end"
    )
    # The year to date a quarter earlier, not any earlier figure.
    assert "OR (period_end - prev_end) BETWEEN 60 AND 120" in sql
    # A tag's priority ranks before reported-over-derived, as in the annual view.
    assert "ORDER BY instrument_id, statement, line, period_end, priority, derived," in sql


def test_share_counts_are_time_weighted(rev0035: ModuleType) -> None:
    # The LIKE pattern must match the mapping's share-count lines, and only those.
    pattern = rev0035.AVERAGE_LINES.strip("'").rstrip("%")
    lines = {r[4] for r in rev0035.statement_mapping()}
    averaged = {line for line in lines if line.startswith(pattern)}
    assert averaged == {"Weighted avg shares (basic)", "Weighted avg shares (diluted)"}
    sql = rev0035.quarterly_view_sql()
    assert (
        "THEN (value * (period_days + 1) - prev_value * (prev_days + 1))\n"
        "                     / (period_end - prev_end)"
    ) in sql


def test_fiscal_year_lookups_use_the_annual_partial_index(rev0035: ModuleType) -> None:
    rev0030 = _load("0030_statement_views_per_instrument.py")
    sql = rev0035.quarterly_view_sql()
    lookups = sql.split("FROM fundamentals a")[1:]
    assert len(lookups) == 2
    for lookup in lookups:
        assert f"AND {rev0030.ANNUAL_PREDICATE.replace('period', 'a.period')}" in lookup


def test_periods_view_tags_each_branch_with_a_constant_frequency(rev0035: ModuleType) -> None:
    # A constant per branch lets a `freq = '...'` filter drop the other one.
    sql = rev0035.PERIODS_VIEW
    assert "'annual'::text AS freq" in sql and "FROM equity_statement_annual a" in sql
    assert "'quarterly'::text," in sql and "FROM equity_statement_quarterly q" in sql
    assert "a.fiscal_year::text AS period" in sql
    assert "q.fiscal_year || ' Q' || q.fiscal_quarter" in sql


def test_downgrade_drops_what_upgrade_creates(rev0035: ModuleType) -> None:
    source = (MIGRATIONS / "0035_quarterly_statements.py").read_text()
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    downgrade = source.split("def downgrade()", 1)[1]
    assert "PERIOD_DAYS_STATISTICS" in upgrade
    for name in (
        "equity_statement_quarterly",
        "equity_statement_periods",
        "equity_statement_period_matrix",
    ):
        assert f"DROP VIEW {name}" in downgrade
    assert "DROP STATISTICS fundamentals_period_days" in downgrade
