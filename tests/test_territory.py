"""Offline unit tests for the territory-area reference series (no network, no DB)."""

from __future__ import annotations

from fintracker.housing.regions import regions_at
from fintracker.housing.territory import (
    INDICATOR,
    PERIOD,
    PROVINCE_AREA_KM2,
    area_rows,
)


def _by_code() -> dict[str, float]:
    return {code: value for code, _, _, value in area_rows()}


def test_area_table_matches_province_hierarchy_exactly() -> None:
    # Every province has an area and there are no stray/wrong codes.
    assert set(PROVINCE_AREA_KM2) == {r.code for r in regions_at("prov")}


def test_area_rows_are_well_formed() -> None:
    for _code, indicator, period, value in area_rows():
        assert indicator == INDICATOR
        assert period == PERIOD
        assert value > 0


def test_nation_area_is_sum_of_all_provinces() -> None:
    assert _by_code()["es"] == sum(PROVINCE_AREA_KM2.values())


def test_ccaa_area_is_sum_of_its_provinces() -> None:
    rows = _by_code()
    for ccaa in regions_at("ccaa"):
        provinces = [p for p in regions_at("prov") if p.parent == ccaa.code]
        expected = sum(PROVINCE_AREA_KM2[p.code] for p in provinces)
        assert rows[ccaa.code] == expected


def test_rows_cover_provinces_plus_ccaa_plus_nation() -> None:
    codes = [code for code, *_ in area_rows()]
    assert len(codes) == len(set(codes))  # no duplicate region rows
    assert len(codes) == len(PROVINCE_AREA_KM2) + len(regions_at("ccaa")) + 1
