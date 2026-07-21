"""Offline unit tests for the Spain housing pipeline (no network, no DB)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from fintracker.housing.indicators import INDICATORS_BY_CODE
from fintracker.housing.ingest_ine import (
    IneSpec,
    choose_table,
    parse_table,
    rows_from_series,
    series_matches,
    series_region,
)
from fintracker.housing.ingest_mivau import MivauSpec, parse_period, rows_from_frame
from fintracker.housing.regions import (
    all_regions,
    region_code_from_ine_code,
    region_code_from_ine_name,
    regions_at,
    regions_by_code,
)
from fintracker.housing.sample import build_sample_observations

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ms(year: int, month: int, day: int) -> int:
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


def _series(labels: list[str], data: list) -> dict:
    return {
        "Nombre": ". ".join(labels) + ".",
        "MetaData": [{"Nombre": label} for label in labels],
        "Data": data,
    }


# --- region hierarchy --------------------------------------------------------


def test_hierarchy_counts_and_parents() -> None:
    assert len(regions_at("nation")) == 1
    assert len(regions_at("ccaa")) == 19
    assert len(regions_at("prov")) == 52
    assert len(regions_at("muni")) == 8131
    by_code = regions_by_code()
    for region in all_regions():
        if region.parent is not None:
            assert region.parent in by_code, region.code


def test_municipality_parent_is_its_province() -> None:
    madrid = regions_by_code()["muni-28079"]
    assert madrid.name == "Madrid"
    assert madrid.parent == "prov-28"
    assert regions_by_code()["prov-28"].parent == "ccaa-13"


def test_region_code_from_ine_name_ccaa_and_province() -> None:
    assert region_code_from_ine_name("Total Nacional", "ccaa") == "es"
    assert region_code_from_ine_name("Madrid, Comunidad de", "ccaa") == "ccaa-13"
    assert region_code_from_ine_name("Castilla - La Mancha", "ccaa") == "ccaa-08"
    # province index handles inverted / bilingual forms
    assert region_code_from_ine_name("Madrid", "prov") == "prov-28"
    assert region_code_from_ine_name("Coruña, A", "prov") == "prov-15"
    assert region_code_from_ine_name("Alacant/Alicante", "prov") == "prov-03"


def test_region_code_from_ine_code_municipality() -> None:
    assert region_code_from_ine_code("28079", "muni") == "muni-28079"
    assert region_code_from_ine_code("13", "ccaa") == "ccaa-13"
    assert region_code_from_ine_code("99999", "muni") is None  # not a real muni


def test_registry_names_match_geojson() -> None:
    for level, fname in (("ccaa", "spain-ccaa.geojson"), ("prov", "spain-provinces.geojson")):
        geo = json.loads((REPO_ROOT / "grafana" / "geo" / fname).read_text("utf-8"))
        feature_ine = {f["properties"]["ine"]: f["properties"]["name"] for f in geo["features"]}
        for region in regions_at(level):
            assert region.ine in feature_ine, region.code
            assert region.name == feature_ine[region.ine], region.code


# --- INE ingest parsing ------------------------------------------------------


def test_choose_table_matches_keywords_and_excludes() -> None:
    spec = IneSpec("poblacion", "EPOB", ("poblacion", "provincia"), "prov", "A")
    tables = [
        {"Id": 1, "Nombre": "Población por comunidades autónomas"},
        {"Id": 2, "Nombre": "Población por provincias y grupos de edad"},
        {"Id": 3, "Nombre": "Población por provincias y sexo"},
    ]
    assert choose_table(tables, spec) == "3"  # 2 excluded by "grupo"


def test_series_region_by_name_and_code() -> None:
    prov = _series(["Cifras de población", "Madrid", "Total"], [])
    assert series_region(prov, "prov") == "prov-28"
    muni = _series(["Renta media por persona", "28079 Madrid"], [])
    assert series_region(muni, "muni") == "muni-28079"


def test_series_matches_filters_and_skips_variation() -> None:
    spec = IneSpec("poblacion", "EPOB", (), "prov", "A", value_filters=("total",))
    assert series_matches(_series(["Madrid", "Total"], []), spec) is True
    assert series_matches(_series(["Madrid", "Hombres"], []), spec) is False  # no "total"
    var = IneSpec("x", "Y", (), "prov", "A")
    assert series_matches(_series(["Madrid", "Variación anual"], []), var) is False


def test_rows_from_series_normalises_period() -> None:
    series = _series(["x"], [
        {"Fecha": _ms(2022, 6, 15), "Valor": 10.0},  # annual -> 2022-01-01
        {"Fecha": _ms(2023, 1, 1), "Valor": 12.0},
        {"Fecha": _ms(2023, 3, 1), "Valor": None},   # dropped
    ])
    assert rows_from_series(series, "A") == [
        (dt.date(2022, 1, 1), 10.0),
        (dt.date(2023, 1, 1), 12.0),
    ]
    q = _series(["x"], [{"Fecha": _ms(2024, 5, 1), "Valor": 5.0}])
    assert rows_from_series(q, "Q") == [(dt.date(2024, 4, 1), 5.0)]


def test_parse_table_end_to_end() -> None:
    spec = IneSpec("poblacion", "EPOB", (), "prov", "A", value_filters=("total",))
    table = [
        _series(["Madrid", "Total"], [{"Fecha": _ms(2023, 1, 1), "Valor": 6700000.0}]),
        _series(["Barcelona", "Total"], [{"Fecha": _ms(2023, 1, 1), "Valor": 5700000.0}]),
        _series(["Madrid", "Variación"], [{"Fecha": _ms(2023, 1, 1), "Valor": 1.2}]),  # skipped
    ]
    rows = parse_table(table, spec)
    assert ("prov-28", dt.date(2023, 1, 1), 6700000.0) in rows
    assert ("prov-08", dt.date(2023, 1, 1), 5700000.0) in rows
    assert len(rows) == 2


# --- MIVAU spreadsheet parsing -----------------------------------------------


def test_parse_period_formats() -> None:
    assert parse_period("2024T1") == dt.date(2024, 1, 1)
    assert parse_period("2024T4") == dt.date(2024, 10, 1)
    assert parse_period("3T2023") == dt.date(2023, 7, 1)
    assert parse_period("2020") == dt.date(2020, 1, 1)
    assert parse_period("Provincia") is None


def test_rows_from_frame_wide_table() -> None:
    frame = pd.DataFrame(
        {
            "Provincia": ["Madrid", "Barcelona", "NoSuchPlace"],
            "2023T4": [3200.0, 2600.0, 1.0],
            "2024T1": [3300.0, None, 2.0],
        }
    )
    rows = rows_from_frame(frame, "prov")
    assert ("prov-28", dt.date(2023, 10, 1), 3200.0) in rows
    assert ("prov-28", dt.date(2024, 1, 1), 3300.0) in rows
    assert ("prov-08", dt.date(2023, 10, 1), 2600.0) in rows
    # NaN cell dropped; unknown region dropped
    assert all(r[0] in ("prov-28", "prov-08") for r in rows)


def test_mivau_spec_defaults() -> None:
    spec = MivauSpec("price_eur_m2", "MIVAU_PRICE_URL", "prov")
    assert spec.sheet == 0 and spec.header_row == 0


# --- sample data -------------------------------------------------------------


def test_sample_observations_cover_indicators() -> None:
    obs = build_sample_observations()
    indicators = {o[1] for o in obs}
    for code in ("price_eur_m2", "price_eur_m2_new", "poblacion", "renta_persona", "densidad"):
        assert code in indicators, code
        assert code in INDICATORS_BY_CODE
    # every sampled value is a finite number
    assert all(isinstance(o[3], float) for o in obs)
    # provinces are covered (map default level)
    prov_codes = {o[0] for o in obs if o[0].startswith("prov-")}
    assert len(prov_codes) == 52
