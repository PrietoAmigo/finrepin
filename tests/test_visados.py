"""Offline unit tests for the visados (building-permits) parser (no network, no DB)."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from fintracker.housing.ingest_visados import (
    _month,
    _province_url,
    _total_viviendas,
    parse_visados_sheet,
)

# A miniature of the real table-21 sheet (Madrid): title/header rows, an annual
# summary block (year in col 0, NO month in col 2), a blank row, then the monthly
# history (year carried down col 0, Spanish month in col 2, total viviendas in
# the right-most column, superficie-media floats in cols 6-7).
_NAN = float("nan")
_SHEET = pd.DataFrame(
    [
        [_NAN, "21.- VISADOS DE DIRECCIÓN DE OBRA NUEVA", _NAN, _NAN, _NAN, _NAN,
         _NAN, _NAN, _NAN, _NAN, _NAN, _NAN],
        [_NAN, "Madrid", _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN],
        # annual block — must be ignored (no month in col 2)
        [2025, _NAN, _NAN, 872, 17695, 1, 227.04, 94.89, _NAN, _NAN, 1185, 19753],
        [2024, _NAN, _NAN, 1085, 17527, 2, 254.43, 106.67, _NAN, _NAN, 1422, 20036],
        [_NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN],
        # monthly history — year present only on the first month of each year
        [2026, _NAN, "Feb", 78, 1357, 0, 169.47, 99.68, _NAN, _NAN, 77, 1512],
        [_NAN, _NAN, "Ene", 106, 2031, 0, 207.50, 106.72, _NAN, _NAN, 72, 2209],
        [2025, _NAN, "Dic", 30, 272, 0, 172.10, 114.88, _NAN, _NAN, 42, 344],
    ]
)


def test_parse_reads_monthly_totals_and_skips_annual_block() -> None:
    assert parse_visados_sheet(_SHEET) == [
        (dt.date(2025, 12, 1), 344.0),
        (dt.date(2026, 1, 1), 2209.0),
        (dt.date(2026, 2, 1), 1512.0),
    ]


def test_annual_year_does_not_bleed_into_the_first_month() -> None:
    # The annual block ends on 2024; the first monthly row (2026-Feb) must take
    # its own year, not carry 2024 forward.
    periods = [period for period, _ in parse_visados_sheet(_SHEET)]
    assert dt.date(2026, 2, 1) in periods
    assert all(p.year in (2025, 2026) for p in periods)


def test_month_parses_spanish_abbreviations() -> None:
    assert _month("Ene") == 1
    assert _month("dic") == 12
    assert _month("Sep") == _month("Set") == 9
    assert _month(float("nan")) is None
    assert _month("Madrid") is None


def test_total_viviendas_prefers_rightmost_whole_number() -> None:
    # picks the total (1512), never a superficie-media float
    assert _total_viviendas([78, 1357, 0, 169.47, 99.68, _NAN, _NAN, 77, 1512]) == 1512.0
    # total column blank → falls back to the last whole count, not the mean area
    assert _total_viviendas([10, 20, 150.5, _NAN]) == 20.0
    # only means present → nothing to take
    assert _total_viviendas([150.5, 99.9]) is None


def test_province_url_uses_the_ine_code() -> None:
    base = "https://apps.fomento.gob.es/Boletinonline/sedal"
    assert _province_url(28, base) == f"{base}/09032810.XLS"  # Madrid
    assert _province_url(1, base) == f"{base}/09030110.XLS"   # Álava
