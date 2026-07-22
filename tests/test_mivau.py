"""Offline tests for the MIVAU price parser.

The synthetic sheets mirror the real "BoletinOnline" .XLS layout: a two-row
header (a year row like "Año 1995" over a quarter row "1º 2º 3º 4º"), region
names down a column, €/m² values in the grid. No network or Excel file needed.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from fintracker.housing import ingest_mivau as m

NAN = float("nan")


def _two_row_sheet() -> pd.DataFrame:
    """Two years (1995, 1996) × four quarters, three regions. Real MIVAU shape."""
    rows = [
        [NAN] * 10,
        [NAN] * 10,
        [NAN, "Tabla 1"] + [NAN] * 8,
        [NAN, "Valor tasado medio de vivienda libre"] + [NAN] * 8,
        [NAN, NAN, "Año 1995", NAN, NAN, NAN, "Año 1996", NAN, NAN, NAN],
        [NAN, NAN, "(trimestre)", NAN, NAN, NAN, "(trimestre)", NAN, NAN, NAN],
        [NAN, NAN, "1º", "2º", "3º", "4º", "1º", "2º", "3º", "4º"],
        [NAN, "Total Nacional", 700, 710, 720, 730, 750, 760, 770, 780],
        [NAN, "Madrid", 1200, 1210, 1220, 1230, 1250, 1260, 1270, 1280],
        [NAN, "Albacete", 600, 610, 620, 630, 640, 650, 660, 670],
    ]
    return pd.DataFrame(rows)


class TestHeaderCells:
    def test_parse_year(self) -> None:
        assert m._parse_year("Año 1995") == 1995
        assert m._parse_year(1996.0) == 1996
        assert m._parse_year("2024") == 2024
        assert m._parse_year("12") is None
        assert m._parse_year("total") is None
        assert m._parse_year(NAN) is None

    def test_parse_quarter(self) -> None:
        assert m._parse_quarter("1º") == 1
        assert m._parse_quarter("4º") == 4
        assert m._parse_quarter("IV") == 4
        assert m._parse_quarter("T3") == 3
        assert m._parse_quarter("2") == 2
        # A year, a price, or a decimal must never read as a quarter.
        assert m._parse_quarter("1995") is None
        assert m._parse_quarter(1834) is None
        assert m._parse_quarter("3.0") is None
        assert m._parse_quarter(NAN) is None


class TestTwoRowHeader:
    def test_detect_quarter_row(self) -> None:
        assert m.detect_quarter_row(_two_row_sheet()) == 6

    def test_column_periods_carries_year_across_quarters(self) -> None:
        periods = m.column_periods(_two_row_sheet(), 6)
        assert periods == {
            2: dt.date(1995, 1, 1),
            3: dt.date(1995, 4, 1),
            4: dt.date(1995, 7, 1),
            5: dt.date(1995, 10, 1),
            6: dt.date(1996, 1, 1),
            7: dt.date(1996, 4, 1),
            8: dt.date(1996, 7, 1),
            9: dt.date(1996, 10, 1),
        }

    def test_rows_from_sheet(self) -> None:
        rows = set(m.rows_from_sheet(_two_row_sheet()))
        # Nation resolves to "es"; Q1 1995 and Q4 1996 read correctly.
        assert ("es", dt.date(1995, 1, 1), 700.0) in rows
        assert ("es", dt.date(1996, 10, 1), 780.0) in rows
        # "Madrid" feeds both the province and the single-province community.
        assert ("prov-28", dt.date(1995, 1, 1), 1200.0) in rows
        assert ("ccaa-13", dt.date(1995, 1, 1), 1200.0) in rows
        # A province with no same-name community maps to the province only.
        assert ("prov-02", dt.date(1995, 7, 1), 620.0) in rows
        assert not any(code.startswith("ccaa") and code != "ccaa-13" for code, _, _ in rows)

    def test_all_periods_present(self) -> None:
        periods = {p for _, p, _ in m.rows_from_sheet(_two_row_sheet())}
        assert len(periods) == 8  # 2 years × 4 quarters


class TestSingleHeaderFallback:
    def test_single_header_row(self) -> None:
        raw = pd.DataFrame([
            [NAN] * 5,
            [NAN, NAN, "2023T1", "2023T2", "2024T1"],
            [NAN, "Madrid", 100, 110, 120],
        ])
        rows = set(m.rows_from_sheet(raw))
        assert ("prov-28", dt.date(2023, 1, 1), 100.0) in rows
        assert ("prov-28", dt.date(2023, 4, 1), 110.0) in rows
        assert ("prov-28", dt.date(2024, 1, 1), 120.0) in rows

    def test_no_header_returns_empty(self) -> None:
        raw = pd.DataFrame([[NAN, "Madrid", "foo"], [NAN, "Albacete", "bar"]])
        assert m.rows_from_sheet(raw) == []
