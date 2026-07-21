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
    choose_tables,
    parse_table,
    rows_from_series,
    series_matches,
    series_region,
)
from fintracker.housing.ingest_mivau import (
    MivauSpec,
    _read_raw,
    _to_float,
    detect_header_row,
    frame_from_raw,
    parse_period,
    rows_from_frame,
)
from fintracker.housing.regions import (
    all_regions,
    region_code_from_ine_code,
    region_code_from_ine_name,
    region_codes_for_name,
    regions_at,
    regions_by_code,
)
from fintracker.housing.sample import _RENTA_ANCHORS, _interp, build_sample_observations
from fintracker.housing.store import dedupe_observations

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


def test_region_codes_for_name_spans_levels() -> None:
    # A single-province community name feeds both its province and community.
    assert set(region_codes_for_name("Madrid")) == {"prov-28", "ccaa-13"}
    # A multi-province community name feeds only the community.
    assert region_codes_for_name("Andalucía") == ["ccaa-01"]
    # A plain province name feeds only the province.
    assert region_codes_for_name("Barcelona") == ["prov-08"]
    # The national total.
    assert region_codes_for_name("Total Nacional") == ["es"]


def test_registry_names_match_geojson() -> None:
    for level, fname in (("ccaa", "spain-ccaa.geojson"), ("prov", "spain-provinces.geojson")):
        geo = json.loads((REPO_ROOT / "grafana" / "geo" / fname).read_text("utf-8"))
        feature_ine = {f["properties"]["ine"]: f["properties"]["name"] for f in geo["features"]}
        for region in regions_at(level):
            assert region.ine in feature_ine, region.code
            assert region.name == feature_ine[region.ine], region.code


# --- INE ingest parsing ------------------------------------------------------


def test_choose_table_matches_keywords_and_excludes() -> None:
    spec = IneSpec("poblacion", "prov", "A", operation="EPOB",
                   keywords=("poblacion", "provincia"))
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
    spec = IneSpec("poblacion", "prov", "A", value_filters=("total",))
    assert series_matches(_series(["Madrid", "Total"], []), spec) is True
    assert series_matches(_series(["Madrid", "Hombres"], []), spec) is False  # no "total"
    var = IneSpec("x", "prov", "A")
    assert series_matches(_series(["Madrid", "Variación anual"], []), var) is False


def test_series_matches_exclude_values_drops_sex_splits() -> None:
    # Population table 2852 is provinces × sex; exclude_values keeps the total.
    spec = IneSpec("poblacion", "prov", "A", exclude_values=("hombres", "mujeres"))
    assert series_matches(_series(["Madrid", "Total"], []), spec) is True
    assert series_matches(_series(["Madrid", "Hombres"], []), spec) is False
    assert series_matches(_series(["Madrid", "Mujeres"], []), spec) is False


def test_renta_value_filter_selects_the_intended_measure() -> None:
    # The ADRH renta table carries several measures; the filter picks one.
    persona = IneSpec("renta_persona", "prov", "A",
                      value_filters=("renta neta media por persona",))
    hogar = IneSpec("renta_hogar", "prov", "A",
                    value_filters=("renta neta media por hogar",))
    per_series = _series(["Madrid", "Renta neta media por persona"], [])
    hog_series = _series(["Madrid", "Renta neta media por hogar"], [])
    assert series_matches(per_series, persona) is True
    assert series_matches(hog_series, persona) is False
    assert series_matches(hog_series, hogar) is True
    assert series_matches(per_series, hogar) is False


def test_choose_tables_returns_all_matches() -> None:
    spec = IneSpec("renta_persona", "muni", "A", operation="ADRH",
                   keywords=("renta", "municipios"), all_tables=True,
                   exclude=("distrito", "grupo"))
    tables = [
        {"Id": 31097, "Nombre": "Renta por municipios. Madrid"},
        {"Id": 30896, "Nombre": "Renta por municipios. Barcelona"},
        {"Id": 99, "Nombre": "Renta por municipios y distritos. Madrid"},  # excluded
        {"Id": 7, "Nombre": "Población por municipios"},                   # no "renta"
    ]
    assert choose_tables(tables, spec) == ["31097", "30896"]
    assert choose_table(tables, spec) == "31097"


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
    spec = IneSpec("poblacion", "prov", "A", value_filters=("total",))
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


def test_parse_period_separators_and_roman() -> None:
    assert parse_period("2024 T1") == dt.date(2024, 1, 1)
    assert parse_period("2024-Q1") == dt.date(2024, 1, 1)
    assert parse_period("2024TIV") == dt.date(2024, 10, 1)
    assert parse_period("2024TIII") == dt.date(2024, 7, 1)
    assert parse_period("2Q 2023") == dt.date(2023, 4, 1)
    assert parse_period("2020.0") == dt.date(2020, 1, 1)  # float-formatted header cell


def test_parse_period_rejects_implausible_years() -> None:
    # 4-digit *values* (€/m² prices) must not be read as years, or header
    # detection could pick a data row.
    assert parse_period("1830.0") is None
    assert parse_period("1650") is None
    assert parse_period("2500") is None
    assert parse_period("28079") is None  # INE municipality code
    assert parse_period("2024 1") is None  # a bare space is not a quarter marker


def test_rows_from_frame_multi_level() -> None:
    frame = pd.DataFrame(
        {
            "Ámbito": ["Total Nacional", "Andalucía", "Madrid", "Barcelona", "NoSuchPlace"],
            "2023T4": [1800.0, 1300.0, 3200.0, 2600.0, 1.0],
            "2024T1": [1850.0, 1350.0, 3300.0, None, 2.0],
        }
    )
    rows = rows_from_frame(frame)
    assert ("es", dt.date(2023, 10, 1), 1800.0) in rows           # nation
    assert ("ccaa-01", dt.date(2023, 10, 1), 1300.0) in rows      # community
    assert ("prov-28", dt.date(2023, 10, 1), 3200.0) in rows      # province Madrid
    assert ("ccaa-13", dt.date(2023, 10, 1), 3200.0) in rows      # Madrid also feeds its CCAA
    assert ("prov-08", dt.date(2023, 10, 1), 2600.0) in rows      # Barcelona (province only)
    assert ("ccaa-08", dt.date(2024, 1, 1), 3200.0) not in rows   # NaN cell dropped
    assert all(not r[0].startswith("muni") for r in rows)         # NoSuchPlace dropped


def test_detect_header_row_skips_title_rows() -> None:
    raw = pd.DataFrame(
        [
            ["Precio medio de la vivienda (€/m²)", None, None],  # title
            ["Serie histórica", None, None],                     # subtitle
            ["Provincia", "2023T4", "2024T1"],                   # header row (index 2)
            ["Madrid", 3200.0, 3300.0],
        ]
    )
    assert detect_header_row(raw) == 2
    frame = frame_from_raw(raw)
    assert frame is not None
    assert ("prov-28", dt.date(2024, 1, 1), 3300.0) in rows_from_frame(frame)


def test_mivau_spec_defaults() -> None:
    spec = MivauSpec("price_eur_m2", "MIVAU_PRICE_URL", "http://x/35101000.XLS")
    assert spec.sheet == 0
    assert spec.default_url.endswith("35101000.XLS")


def test_detect_header_row_not_fooled_by_price_like_cells() -> None:
    # Data rows full of 4-digit prices ("1830.0") must not out-score the header.
    raw = pd.DataFrame(
        [
            ["Precio medio", None, None, None],
            ["Provincia", "2023T4", "2024T1", "2024T2"],
            ["Madrid", 3210.0, 3300.0, 3350.0],
            ["Burgos", 1830.0, 1840.0, 1850.0],
            ["Cuenca", 1650.0, 1655.0, 1660.0],
        ]
    )
    assert detect_header_row(raw) == 1


def test_rows_from_frame_skips_leading_blank_column() -> None:
    # Sheets can carry a blank filler column before the region names; the region
    # column is the one whose values resolve, not blindly the first non-period.
    frame = pd.DataFrame(
        {
            "nan": [None, None],
            "Provincia": ["Madrid", "Barcelona"],
            "2024T1": [3300.0, 2700.0],
        }
    )
    rows = rows_from_frame(frame)
    assert ("prov-28", dt.date(2024, 1, 1), 3300.0) in rows
    assert ("prov-08", dt.date(2024, 1, 1), 2700.0) in rows


def test_duplicate_region_rows_collapse_before_upsert() -> None:
    # One sheet lists "Madrid, Comunidad de" (→ ccaa-13) and "Madrid" (province,
    # → prov-28 AND ccaa-13): ccaa-13 appears twice, which a single INSERT ..
    # ON CONFLICT DO UPDATE would reject. The store dedupes first.
    frame = pd.DataFrame(
        {
            "Ámbito": ["Madrid, Comunidad de", "Madrid"],
            "2024T1": [3300.0, 3300.0],
        }
    )
    parsed = rows_from_frame(frame)
    rows = [(region, "price_eur_m2", period, value) for region, period, value in parsed]
    assert len(rows) == 3  # ccaa-13 twice + prov-28 once
    deduped = dedupe_observations(rows)
    assert sorted(r[0] for r in deduped) == ["ccaa-13", "prov-28"]


def test_dedupe_observations_last_value_wins() -> None:
    rows = [
        ("prov-28", "price_eur_m2", dt.date(2024, 1, 1), 1.0),
        ("prov-28", "price_eur_m2", dt.date(2024, 1, 1), 2.0),
        ("prov-08", "price_eur_m2", dt.date(2024, 1, 1), 9.0),
    ]
    assert sorted(dedupe_observations(rows)) == [
        ("prov-08", "price_eur_m2", dt.date(2024, 1, 1), 9.0),
        ("prov-28", "price_eur_m2", dt.date(2024, 1, 1), 2.0),
    ]


def test_to_float_handles_spanish_locale_strings() -> None:
    assert _to_float("1.834,5") == 1834.5
    assert _to_float("1834.5") == 1834.5
    assert _to_float("\xa02.050,3 ") == 2050.3  # non-breaking + regular spaces
    assert _to_float(1834) == 1834.0
    assert _to_float("n.d.") is None
    assert _to_float(None) is None
    assert _to_float(float("nan")) is None


def test_read_raw_html_fallback_recovers_promoted_header() -> None:
    # Some ministry ".XLS" downloads are HTML; read_html promotes <th> cells to
    # column labels, which _read_raw pushes back down so header detection works.
    html = """
    <table>
      <thead><tr><th>Provincia</th><th>2023T4</th><th>2024T1</th></tr></thead>
      <tbody>
        <tr><td>Madrid</td><td>3.210,4</td><td>3.300,0</td></tr>
        <tr><td>Barcelona</td><td>2.600,0</td><td>2.700,5</td></tr>
      </tbody>
    </table>
    """
    raw = _read_raw(html.encode(), "http://x/35101000.XLS")
    assert raw is not None
    frame = frame_from_raw(raw)
    assert frame is not None
    rows = rows_from_frame(frame)
    assert ("prov-28", dt.date(2023, 10, 1), 3210.4) in rows
    assert ("prov-08", dt.date(2024, 1, 1), 2700.5) in rows


# --- sample data -------------------------------------------------------------


def test_interp_clamps_beyond_anchor_range() -> None:
    # Years past the last anchor hold the last value (they used to snap back to
    # the FIRST anchor, plunging the final sample year).
    last_year = max(_RENTA_ANCHORS)
    assert _interp(_RENTA_ANCHORS, dt.date(last_year + 1, 1, 1)) == _RENTA_ANCHORS[last_year]
    assert _interp(_RENTA_ANCHORS, dt.date(1990, 1, 1)) == _RENTA_ANCHORS[min(_RENTA_ANCHORS)]


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
