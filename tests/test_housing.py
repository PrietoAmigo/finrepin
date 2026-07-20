"""Offline unit tests for the Spain housing dashboard (no network, no DB)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fintracker.housing.dataset import assemble_dataset
from fintracker.housing.ingest import (
    choose_ccaa_table,
    classify_series,
    parse_table,
    rows_from_data,
)
from fintracker.housing.regions import (
    REGIONS,
    REGIONS_BY_CODE,
    region_code_from_ine_name,
)
from fintracker.housing.sample import build_sample_dataset

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ms(year: int, month: int, day: int) -> int:
    """Epoch milliseconds (UTC) for an INE ``Fecha`` value."""
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


def _series(geo: str, geo_var: str, component: str, metric: str, data: list) -> dict:
    return {
        "Nombre": f"Índice de Precios de Vivienda. {geo}. {component}. {metric}.",
        "MetaData": [
            {"Nombre": geo, "Variable": {"Nombre": geo_var}},
            {"Nombre": component, "Variable": {"Nombre": "General y componentes"}},
            {"Nombre": metric, "Variable": {"Nombre": "Índices y tasas de variación"}},
        ],
        "Data": data,
    }


# --- region name matching ----------------------------------------------------


def test_region_code_from_ine_name_maps_all_communities() -> None:
    # INE uses inverted/variant names; every one must resolve to its CCAA code.
    cases = {
        "Total Nacional": "es",
        "Nacional": "es",
        "Andalucía": "ccaa-01",
        "Aragón": "ccaa-02",
        "Asturias, Principado de": "ccaa-03",
        "Balears, Illes": "ccaa-04",
        "Canarias": "ccaa-05",
        "Cantabria": "ccaa-06",
        "Castilla y León": "ccaa-07",
        "Castilla - La Mancha": "ccaa-08",
        "Cataluña": "ccaa-09",
        "Comunitat Valenciana": "ccaa-10",
        "Extremadura": "ccaa-11",
        "Galicia": "ccaa-12",
        "Madrid, Comunidad de": "ccaa-13",
        "Murcia, Región de": "ccaa-14",
        "Navarra, Comunidad Foral de": "ccaa-15",
        "País Vasco": "ccaa-16",
        "Rioja, La": "ccaa-17",
        "Ceuta": "ccaa-18",
        "Melilla": "ccaa-19",
    }
    for name, code in cases.items():
        assert region_code_from_ine_name(name) == code, name


def test_castilla_variants_are_not_confused() -> None:
    assert region_code_from_ine_name("Castilla y León") == "ccaa-07"
    assert region_code_from_ine_name("Castilla-La Mancha") == "ccaa-08"


def test_operation_title_does_not_match_a_region() -> None:
    # "nacionales por comunidades autónomas" must NOT be read as the nation.
    assert region_code_from_ine_name("Índices nacionales por comunidades autónomas") is None


# --- series classification ---------------------------------------------------


def test_classify_series_components_and_nation() -> None:
    nacional = _series(
        "Total Nacional", "Comunidades y Ciudades Autónomas", "General", "Índice", []
    )
    assert classify_series(nacional) == ("es", "ipv_general")

    madrid_new = _series(
        "Madrid, Comunidad de", "Comunidades y Ciudades Autónomas", "Vivienda nueva", "Índice", []
    )
    assert classify_series(madrid_new) == ("ccaa-13", "ipv_new")

    cat_resale = _series(
        "Cataluña", "Comunidades y Ciudades Autónomas", "Vivienda de segunda mano", "Índice", []
    )
    assert classify_series(cat_resale) == ("ccaa-09", "ipv_secondhand")


def test_classify_series_skips_variation() -> None:
    variation = _series(
        "Andalucía", "Comunidades y Ciudades Autónomas", "General", "Variación anual", []
    )
    assert classify_series(variation) is None


def test_classify_series_skips_unknown_component() -> None:
    # A geographic total with no recognised component is not stored.
    weird = _series("Andalucía", "Comunidades y Ciudades Autónomas", "Otros", "Índice", [])
    assert classify_series(weird) is None


# --- data extraction ---------------------------------------------------------


def test_rows_from_data_normalises_to_quarter_start() -> None:
    data = [
        {"Fecha": _ms(2024, 2, 15), "Valor": 176.7},  # mid-Q1 -> 2024-01-01
        {"Fecha": _ms(2024, 5, 1), "Valor": 179.2},  # Q2 -> 2024-04-01
        {"Fecha": _ms(2024, 6, 30), "Valor": None},  # dropped
    ]
    rows = rows_from_data(data)
    assert rows == [(dt.date(2024, 1, 1), 176.7), (dt.date(2024, 4, 1), 179.2)]


def test_parse_table_flattens_and_filters() -> None:
    table = [
        _series(
            "Total Nacional", "Comunidades y Ciudades Autónomas", "General", "Índice",
            [{"Fecha": _ms(2024, 1, 1), "Valor": 176.7}],
        ),
        _series(
            "Andalucía", "Comunidades y Ciudades Autónomas", "General", "Índice",
            [{"Fecha": _ms(2024, 1, 1), "Valor": 150.0}],
        ),
        _series(
            "Andalucía", "Comunidades y Ciudades Autónomas", "General", "Variación anual",
            [{"Fecha": _ms(2024, 1, 1), "Valor": 4.2}],
        ),
    ]
    rows = parse_table(table)
    assert ("es", "ipv_general", dt.date(2024, 1, 1), 176.7) in rows
    assert ("ccaa-01", "ipv_general", dt.date(2024, 1, 1), 150.0) in rows
    assert len(rows) == 2  # the variation series is excluded


# --- table discovery ---------------------------------------------------------


def test_choose_ccaa_table_prefers_community_components_table() -> None:
    tables = [
        {"Id": 100, "Nombre": "Índices nacionales: general y componentes"},
        {"Id": 200, "Nombre": "Índices por comunidades autónomas: general y componentes"},
        {"Id": 300, "Nombre": "Índices por comunidades autónomas y grupos"},
    ]
    assert choose_ccaa_table(tables) == "200"


def test_choose_ccaa_table_returns_none_without_match() -> None:
    assert choose_ccaa_table([{"Id": 1, "Nombre": "Índices nacionales por grupos"}]) is None


# --- dataset shaping ---------------------------------------------------------


def test_assemble_dataset_aligns_series_to_periods() -> None:
    obs = [
        ("ipv_general", "es", dt.date(2024, 1, 1), 100.0),
        ("ipv_general", "es", dt.date(2024, 4, 1), 101.0),
        ("ipv_general", "ccaa-13", dt.date(2024, 4, 1), 200.0),  # missing Q1
    ]
    ds = assemble_dataset(obs, mode="live", note="x")
    assert ds["periods"]["ipv_general"] == ["2024-01-01", "2024-04-01"]
    assert ds["series"]["ipv_general"]["es"] == [100.0, 101.0]
    assert ds["series"]["ipv_general"]["ccaa-13"] == [None, 200.0]  # gap padded
    assert ds["updated_at"] == "2024-04-01"
    assert ds["levels"] == ["ccaa"]


def test_sample_dataset_is_complete_and_well_formed() -> None:
    ds = build_sample_dataset()
    assert ds["mode"] == "sample"
    assert [i["code"] for i in ds["indicators"]] == ["ipv_general", "ipv_new", "ipv_secondhand"]
    assert len(ds["regions"]["ccaa"]) == 19
    for indicator in ("ipv_general", "ipv_new", "ipv_secondhand"):
        periods = ds["periods"][indicator]
        assert periods == sorted(periods)
        for values in ds["series"][indicator].values():
            assert len(values) == len(periods)
            assert all(v is not None for v in values)  # sample is fully populated


# --- map-join invariant ------------------------------------------------------


def test_registry_names_match_geojson_features() -> None:
    """Every CCAA in the registry must have a same-named GeoJSON polygon.

    This is the join the front-end relies on (ECharts matches map data to
    features by name), so it must never drift.
    """
    geo = json.loads((REPO_ROOT / "web" / "geo" / "spain-ccaa.geojson").read_text("utf-8"))
    feature_names = {f["properties"]["name"] for f in geo["features"]}
    feature_codes = {f["properties"]["code"] for f in geo["features"]}

    ccaa = [r for r in REGIONS if r.level == "ccaa"]
    assert len(ccaa) == len(feature_names) == 19
    for region in ccaa:
        assert region.name in feature_names, region.name
        assert region.code in feature_codes, region.code


def test_region_parents_are_valid() -> None:
    for region in REGIONS:
        if region.parent is not None:
            assert region.parent in REGIONS_BY_CODE, region.code
