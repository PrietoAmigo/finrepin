"""Migration 0034: the pre-2018 and fallback XBRL tags, checked without a database.

The views can only use tags an ingester stores, and a fallback must never
displace a tag the views already used: it joins its family (and statement
line) after the existing tags, so it fills only periods that had none.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from fintracker.ingest import yahoo_fundamentals as yahoo
from fintracker.ingest.fundamentals import CURATED_TAGS, extract_facts

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
FAMILY = re.compile(r"\('(\w+)', '(\w+)', (\d+)\)")
MAPPING = re.compile(r"\('(\w+)', \d+, '[^']*', \d+,\s*'([^']*)', (\d+),\s*'(\w+)'\)")


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{filename}", MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev0034() -> ModuleType:
    return _load("0034_legacy_xbrl_tags.py")


@pytest.fixture(scope="module")
def rev0007() -> ModuleType:
    return _load("0007_currency_views.py")


def _families(text: str) -> list[tuple[str, str, int]]:
    return [(f, t, int(p)) for f, t, p in FAMILY.findall(text)]


def _mapping(text: str) -> list[tuple[str, str, int, str]]:
    rows = [(s, line, int(p), t) for s, line, p, t in MAPPING.findall(text)]
    assert len(rows) == len(re.findall(r"\('(?:income|balance|cashflow)'", text))
    return rows


def test_every_mapped_tag_is_stored(rev0034: ModuleType, rev0007: ModuleType) -> None:
    stored = set(CURATED_TAGS["us-gaap"]) | set(CURATED_TAGS["ifrs-full"])
    stored |= {
        tag
        for lines in (yahoo.INCOME_LINES, yahoo.BALANCE_LINES, yahoo.CASHFLOW_LINES)
        for _, tag, _ in lines
    }
    families = _families(rev0034.families_0008() + rev0034.LEGACY_FAMILIES)
    mapping = _mapping(str(rev0007.STATEMENT_MAPPING) + rev0034.LEGACY_MAPPING)
    assert {t for _, t, _ in families} <= stored
    assert {t for *_, t in mapping} <= stored
    # The new tags are us-gaap ones: stored by the SEC ingest itself.
    legacy = {t for _, t, _ in _families(rev0034.LEGACY_FAMILIES)}
    legacy |= {t for *_, t in _mapping(rev0034.LEGACY_MAPPING)}
    assert legacy <= set(CURATED_TAGS["us-gaap"])


def test_fallbacks_rank_after_existing_tags(rev0034: ModuleType, rev0007: ModuleType) -> None:
    old = _families(rev0034.families_0008())
    for family, tag, priority in _families(rev0034.LEGACY_FAMILIES):
        existing = [p for f, _, p in old if f == family]
        assert existing and priority > max(existing), (family, tag)
        assert tag not in {t for f, t, _ in old if f == family}
    old_lines = _mapping(str(rev0007.STATEMENT_MAPPING))
    for statement, line, priority, tag in _mapping(rev0034.LEGACY_MAPPING):
        existing = [p for s, ln, p, _ in old_lines if (s, ln) == (statement, line)]
        assert existing and priority > max(existing), (line, tag)


@pytest.mark.parametrize(
    ("family", "tag"),
    [
        ("revenue", "SalesRevenueNet"),
        ("revenue", "SalesRevenueServicesNet"),
        ("revenue", "SalesRevenueGoodsNet"),
        ("cogs", "CostOfGoodsSold"),
        ("cogs", "CostOfServices"),
        ("cogs", "CostOfServicesExcludingDepreciationDepletionAndAmortization"),
        ("net_income", "ProfitLoss"),
    ],
)
def test_retired_and_fallback_tags_reach_the_metrics(
    rev0034: ModuleType, family: str, tag: str
) -> None:
    families = _families(rev0034.families_0008() + rev0034.LEGACY_FAMILIES)
    assert (family, tag) in {(f, t) for f, t, _ in families}
    assert tag in CURATED_TAGS["us-gaap"]


def test_upgrade_extends_and_downgrade_restores_the_views(
    rev0034: ModuleType, rev0007: ModuleType
) -> None:
    grouped_up = rev0034.fundamentals_grouped_sql(
        rev0034.families_0008() + "," + rev0034.LEGACY_FAMILIES.rstrip()
    )
    assert "('revenue', 'SalesRevenueNet', 4)" in grouped_up
    assert grouped_up.count("CREATE OR REPLACE VIEW fundamentals_grouped") == 1
    annual_up = rev0034.statement_annual_sql(rev0034.LEGACY_MAPPING)
    assert annual_up.startswith("CREATE OR REPLACE VIEW equity_statement_annual AS")
    assert "'SalesRevenueServicesNet')" in annual_up
    # Downgrade: exactly 0007's annual view and 0008's families again.
    annual_down = rev0034.statement_annual_sql()
    assert annual_down == rev0007._annual_view(with_currency=True).replace(
        "CREATE VIEW", "CREATE OR REPLACE VIEW", 1
    )
    assert "SalesRevenueNet" not in rev0034.fundamentals_grouped_sql(rev0034.families_0008())


def test_sec_ingest_keeps_the_retired_tags() -> None:
    def fy2015(val: float) -> dict[str, object]:
        fact = {"start": "2015-01-01", "end": "2015-12-31", "val": val, "form": "10-K"}
        return {"units": {"USD": [{**fact, "filed": "2016-02-25"}]}}

    payload = {
        "facts": {
            "us-gaap": {
                "SalesRevenueServicesNet": fy2015(9.0),
                "CostOfServices": fy2015(4.0),
                "ProfitLoss": fy2015(1.0),
            }
        }
    }
    facts = extract_facts(payload, "us-gaap", CURATED_TAGS["us-gaap"])
    assert {f["tag"] for f in facts} == {"SalesRevenueServicesNet", "CostOfServices", "ProfitLoss"}
