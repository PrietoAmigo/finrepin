"""Offline tests for the PC-Axis (.px) parser — no network, no real file."""
from __future__ import annotations

import pytest

from fintracker.housing.pcaxis import cell_to_float, parse_px

# Two provinces × two dwelling types (STUB) × two periods (HEADING). The DATA
# matrix is stub-combos down the rows, periods across the columns: 10/95.7
# (Madrid,Total 2011/2021), 20/21 (Madrid,Principal), 30/31 (Barcelona,Total),
# 40/.. (Barcelona,Principal — one decimal and one missing value).
SAMPLE = """CHARSET="ANSI";
AXIS-VERSION="2013";
STUB="Provincias","Tipo de vivienda";
HEADING="Periodo";
VALUES("Provincias")="28 Madrid","08 Barcelona";
VALUES("Tipo de vivienda")="Total","Principal";
VALUES("Periodo")="2011","2021";
UNITS="viviendas";
DATA=
10 95.7
20 21
30 31
40 ..
;
"""


class TestParsePx:
    def test_dimensions_in_data_order(self) -> None:
        table = parse_px(SAMPLE)
        assert table.dims == ("Provincias", "Tipo de vivienda", "Periodo")
        assert table.categories["Provincias"] == ["28 Madrid", "08 Barcelona"]
        assert table.categories["Periodo"] == ["2011", "2021"]

    def test_cells_map_to_the_right_category_tuple(self) -> None:
        cells = dict(parse_px(SAMPLE).cells)
        assert cells[("28 Madrid", "Total", "2011")] == 10.0
        assert cells[("28 Madrid", "Total", "2021")] == 95.7  # decimal (period)
        assert cells[("28 Madrid", "Principal", "2011")] == 20.0
        assert cells[("08 Barcelona", "Total", "2011")] == 30.0
        assert cells[("08 Barcelona", "Principal", "2011")] == 40.0
        assert cells[("08 Barcelona", "Principal", "2021")] is None  # ".." missing

    def test_series_filter(self) -> None:
        table = parse_px(SAMPLE)
        principal = table.series(**{"Tipo de vivienda": "Principal"})
        assert {labels[0] for labels, _ in principal} == {"28 Madrid", "08 Barcelona"}
        assert all(labels[1] == "Principal" for labels, _ in principal)
        assert len(principal) == 4  # 2 provinces × 2 periods

    def test_multiline_values(self) -> None:
        px = (
            'STUB="Prov";\nHEADING="P";\n'
            'VALUES("Prov")="28 Madrid",\n"08 Barcelona";\n'
            'VALUES("P")="2021";\nDATA=\n5 6\n;\n'
        )
        table = parse_px(px)
        assert table.categories["Prov"] == ["28 Madrid", "08 Barcelona"]
        assert dict(table.cells)[("28 Madrid", "2021")] == 5.0


class TestCellToFloat:
    def test_numbers(self) -> None:
        assert cell_to_float("1234") == 1234.0
        assert cell_to_float("95.7") == 95.7        # period decimal (PC-Axis)
        assert cell_to_float("1234,5") == 1234.5     # defensive comma-decimal
        assert cell_to_float('"88"') == 88.0         # stray quotes tolerated

    def test_missing_markers(self) -> None:
        for marker in ("..", ".", "-", ":", ""):
            assert cell_to_float(marker) is None


class TestErrors:
    def test_cell_count_mismatch_raises(self) -> None:
        bad = SAMPLE.replace("40 ..", "40")  # 7 values, expected 8
        with pytest.raises(ValueError, match="expected 8"):
            parse_px(bad)

    def test_missing_dims_raises(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            parse_px('CHARSET="ANSI";\nDATA=\n1\n;\n')

    def test_missing_values_raises(self) -> None:
        px = 'STUB="Prov";\nHEADING="P";\nVALUES("P")="2021";\nDATA=\n1\n;\n'
        with pytest.raises(ValueError, match="VALUES for dimension 'Prov'"):
            parse_px(px)
