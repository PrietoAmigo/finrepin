"""Offline tests for the census (.px) → region-observation mapping."""
from __future__ import annotations

import datetime as dt

from fintracker.housing.ingest_censo import (
    CENSO_SPECS,
    CensoSpec,
    _region_for,
    rows_from_px,
    weighted_mean_from_buckets,
)
from fintracker.housing.pcaxis import parse_px

# A miniature of the real 03004.px: Provincias × Año × periodo × Superficie, with
# only "Total" + two bands per characteristic. Albacete's año=Total row gives the
# surface bands (100 @ <46m², 50 @ >150m²) and its superficie=Total row gives the
# year bands (30 built post-2010, 70 pre-1921); the "Total" province is dropped.
CENSO_03004_SAMPLE = """STUB="Provincias","Año de construcción","periodo";
HEADING="Superficie útil";
VALUES("Provincias")="Total","Albacete";
VALUES("Año de construcción")="Total","Posterior al 2010","Antes de 1921";
VALUES("periodo")="2020";
VALUES("Superficie útil")="Total","Menos de 46 m2","Más de 150 m2";
DATA=
999 999 999
999 999 999
999 999 999
150 100 50
30 9 9
70 9 9
;
"""

VIVIENDAS_PX = """STUB="Provincias","Tipo de vivienda";
HEADING="Periodo";
VALUES("Provincias")="28 Madrid","08 Barcelona";
VALUES("Tipo de vivienda")="Total","Viviendas principales";
VALUES("Periodo")="2021";
DATA=
100 60
80 50
;
"""

SUPERFICIE_PX = """STUB="Provincias","Superficie útil";
HEADING="Periodo";
VALUES("Provincias")="28 Madrid";
VALUES("Superficie útil")="hasta 30 m2","30 - 45 m2","más de 180 m2";
VALUES("Periodo")="2021";
DATA=
10 20 70
;
"""


class TestWeightedMean:
    def test_basic(self) -> None:
        got = weighted_mean_from_buckets(
            [("a", 10.0), ("b", 30.0)], (("a", 1.0), ("b", 2.0))
        )
        assert got == (10 * 1 + 30 * 2) / 40

    def test_unmatched_zero_and_missing_ignored(self) -> None:
        got = weighted_mean_from_buckets(
            [("a", 10.0), ("z", 5.0), ("a", 0.0), ("a", None)], (("a", 2.0),)
        )
        assert got == 2.0  # only the first "a"=10 counts

    def test_empty_returns_none(self) -> None:
        assert weighted_mean_from_buckets([], (("a", 1.0),)) is None
        assert weighted_mean_from_buckets([("z", 9.0)], (("a", 1.0),)) is None


class TestRegionResolution:
    def test_code_prefix_and_name_fallback(self) -> None:
        assert _region_for("28 Madrid", "prov") == "prov-28"
        assert _region_for("08 Barcelona", "prov") == "prov-08"
        assert _region_for("Andalucía", "ccaa") == "ccaa-01"  # no code → name match
        assert _region_for("nonsense", "prov") is None


class TestRowsFromPx:
    def test_direct_count_with_select(self) -> None:
        table = parse_px(VIVIENDAS_PX)
        total = CensoSpec("viviendas_total", "X", "prov", period=dt.date(2021, 1, 1),
                          select=(("Tipo de vivienda", "Total"),))
        assert set(rows_from_px(table, total)) == {
            ("prov-28", dt.date(2021, 1, 1), 100.0),
            ("prov-08", dt.date(2021, 1, 1), 80.0),
        }
        principal = CensoSpec("viviendas_principales", "X", "prov",
                              select=(("Tipo de vivienda", "Viviendas principales"),))
        assert dict((r, v) for r, _, v in rows_from_px(table, principal)) == {
            "prov-28": 60.0, "prov-08": 50.0,
        }

    def test_weighted_mean_from_buckets(self) -> None:
        table = parse_px(SUPERFICIE_PX)
        spec = CensoSpec(
            "superficie_media_m2", "X", "prov",
            bucket_dim="Superficie útil",
            midpoints=(("hasta 30", 25.0), ("30", 38.0), ("más de 180", 200.0)),
        )
        rows = rows_from_px(table, spec)
        assert len(rows) == 1
        code, _, value = rows[0]
        assert code == "prov-28"
        # (10·25 + 20·38 + 70·200) / 100
        assert value == (10 * 25 + 20 * 38 + 70 * 200) / 100

    def test_missing_dimension_yields_nothing(self) -> None:
        table = parse_px(VIVIENDAS_PX)
        spec = CensoSpec("x", "X", "prov", select=(("No such dim", "Total"),))
        assert rows_from_px(table, spec) == []


class TestShippedCensoSpecs:
    """Run the *actually shipped* CENSO_SPECS against a 03004.px-shaped sample.

    Guards the wiring — dimension/category names in ``select``/``bucket_dim`` and
    the midpoint substrings — against the engine, so a rename in one place that
    breaks the other is caught offline. (Whether the midpoint needles match the
    *real* INE labels can only be checked against a live ``.px``.)
    """

    _BY_CODE = {s.indicator: s for s in CENSO_SPECS}

    def test_superficie_media_weighted_over_surface_bands(self) -> None:
        table = parse_px(CENSO_03004_SAMPLE)
        rows = rows_from_px(table, self._BY_CODE["superficie_media_m2"])
        # año=Total, periodo=2020 → Albacete's bands 100 @ 38m² + 50 @ 185m².
        assert rows == [("prov-02", dt.date(2020, 1, 1), (100 * 38 + 50 * 185) / 150)]
        assert rows[0][2] == 87.0

    def test_antiguedad_media_weighted_over_year_bands(self) -> None:
        table = parse_px(CENSO_03004_SAMPLE)
        rows = rows_from_px(table, self._BY_CODE["antiguedad_media"])
        # superficie=Total, periodo=2020 → Albacete's 30 @ age 5 + 70 @ age 110.
        assert rows == [("prov-02", dt.date(2020, 1, 1), (30 * 5 + 70 * 110) / 100)]
        assert rows[0][2] == 78.5

    def test_both_specs_default_on_via_default_url(self) -> None:
        # Census indicators ship enabled: each carries a built-in .px URL so it
        # ingests without an env override.
        for spec in self._BY_CODE.values():
            assert spec.default_url
            assert spec.period == dt.date(2020, 1, 1)
