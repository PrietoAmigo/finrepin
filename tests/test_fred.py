"""Unit tests for the pure FRED CSV parser — no network, no database."""

from __future__ import annotations

import datetime as dt

from fintracker.ingest.fred import rows_from_csv

# A fredgraph.csv sample: header row, a real observation, a missing one ('.'),
# a blank one, and an out-of-order row to prove sorting.
SAMPLE = (
    "DATE,DGS10\n"
    "2026-05-01,4.52\n"
    "2026-06-01,.\n"
    "2026-04-01,4.31\n"
    "2026-07-01,\n"
    "2026-08-01,4.60\n"
)


class TestRowsFromCsv:
    def test_skips_missing_and_blank_values(self) -> None:
        dates = [r["date"] for r in rows_from_csv(SAMPLE)]
        assert dates == [
            dt.date(2026, 4, 1),
            dt.date(2026, 5, 1),
            dt.date(2026, 8, 1),
        ]

    def test_rows_are_sorted_oldest_first(self) -> None:
        rows = rows_from_csv(SAMPLE)
        assert rows[0]["date"] < rows[1]["date"] < rows[2]["date"]

    def test_value_lands_in_close_only(self) -> None:
        first = rows_from_csv(SAMPLE)[0]
        assert first["close"] == 4.31
        assert first["open"] is None
        assert first["high"] is None
        assert first["low"] is None
        assert first["volume"] is None

    def test_empty_or_header_only_input_yields_no_rows(self) -> None:
        assert rows_from_csv("") == []
        assert rows_from_csv("DATE,DGS10\n") == []

    def test_non_numeric_value_is_skipped(self) -> None:
        rows = rows_from_csv("DATE,X\n2026-05-01,oops\n2026-05-02,1.5\n")
        assert [r["date"] for r in rows] == [dt.date(2026, 5, 2)]
