"""Offline tests for the census (.px) → region-observation mapping."""
from __future__ import annotations

import datetime as dt

from fintracker.housing.ingest_censo import (
    CensoSpec,
    _region_for,
    rows_from_px,
    weighted_mean_from_buckets,
)
from fintracker.housing.pcaxis import parse_px

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
