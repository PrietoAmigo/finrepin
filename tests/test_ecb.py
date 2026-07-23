"""Unit tests for the pure ECB CSV parser — no network, no database."""

from __future__ import annotations

import datetime as dt

from fintracker.ingest.ecb import _parse_ecb_period, rows_from_ecb_csv

# An ECB `csvdata` response: the SDMX dimension columns, then TIME_PERIOD and
# OBS_VALUE (with trailing attribute columns). Includes an out-of-order row, a
# blank value, and a non-numeric value to exercise the skips and the sort.
KEY = "YC.B.U2.EUR.4F.G_N_C.SV_C_YM.SR_10Y"
SAMPLE = (
    "KEY,FREQ,REF_AREA,CURRENCY,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    f"{KEY},B,U2,EUR,2026-07-15,2.63,A\n"
    f"{KEY},B,U2,EUR,2026-07-13,2.59,A\n"
    f"{KEY},B,U2,EUR,2026-07-16,,A\n"
    f"{KEY},B,U2,EUR,2026-07-17,n/a,A\n"
    f"{KEY},B,U2,EUR,2026-07-14,2.61,A\n"
)


class TestRowsFromEcbCsv:
    def test_locates_columns_by_name_and_sorts_oldest_first(self) -> None:
        dates = [r["date"] for r in rows_from_ecb_csv(SAMPLE)]
        assert dates == [dt.date(2026, 7, 13), dt.date(2026, 7, 14), dt.date(2026, 7, 15)]

    def test_blank_and_non_numeric_values_are_skipped(self) -> None:
        # 2026-07-16 (blank) and 2026-07-17 (n/a) drop out.
        values = [r["close"] for r in rows_from_ecb_csv(SAMPLE)]
        assert values == [2.59, 2.61, 2.63]

    def test_value_lands_in_close_only(self) -> None:
        first = rows_from_ecb_csv(SAMPLE)[0]
        assert first["close"] == 2.59
        assert first["open"] is None and first["high"] is None
        assert first["low"] is None and first["volume"] is None

    def test_column_order_is_not_assumed(self) -> None:
        # OBS_VALUE before TIME_PERIOD, extra leading columns — still parsed.
        csv_text = (
            "OBS_VALUE,KEY,TIME_PERIOD\n"
            "1.5,X,2026-01-02\n"
            "1.6,X,2026-01-03\n"
        )
        rows = rows_from_ecb_csv(csv_text)
        assert [(r["date"], r["close"]) for r in rows] == [
            (dt.date(2026, 1, 2), 1.5),
            (dt.date(2026, 1, 3), 1.6),
        ]

    def test_missing_expected_columns_yields_no_rows(self) -> None:
        assert rows_from_ecb_csv("FOO,BAR\n1,2\n") == []
        assert rows_from_ecb_csv("") == []

    def test_monthly_series_parse(self) -> None:
        # A monthly series (e.g. Euribor) uses "YYYY-MM"; each maps to the 1st.
        monthly = (
            "KEY,TIME_PERIOD,OBS_VALUE\n"
            "X,2026-05,3.10\n"
            "X,2026-06,3.05\n"
        )
        assert [(r["date"], r["close"]) for r in rows_from_ecb_csv(monthly)] == [
            (dt.date(2026, 5, 1), 3.10),
            (dt.date(2026, 6, 1), 3.05),
        ]


class TestParseEcbPeriod:
    def test_frequencies(self) -> None:
        assert _parse_ecb_period("2024-06-15") == dt.date(2024, 6, 15)  # daily
        assert _parse_ecb_period("2024-06") == dt.date(2024, 6, 1)      # monthly
        assert _parse_ecb_period("2024-Q2") == dt.date(2024, 4, 1)      # quarterly
        assert _parse_ecb_period("2024") == dt.date(2024, 1, 1)         # annual

    def test_bad_values_return_none(self) -> None:
        assert _parse_ecb_period("not-a-date") is None
        assert _parse_ecb_period("2024-13") is None
