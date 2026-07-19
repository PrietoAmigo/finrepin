"""Unit tests for the pure Coin Metrics parser — no network, no database."""

from __future__ import annotations

import datetime as dt

from fintracker.ingest.coinmetrics import rows_from_metric

METRIC = "CapMrktCurUSD"

# A Coin Metrics asset-metrics sample: two real points, an out-of-order one (to
# prove sorting), a point missing the metric key, an empty-string value, a
# non-numeric value, and a point with no timestamp — all but the three real
# observations must be dropped. Timestamps carry the API's nanosecond precision.
DATA = [
    {"asset": "btc", "time": "2010-07-18T00:00:00.000000000Z", METRIC: "1000.0"},
    {"asset": "btc", "time": "2010-07-20T00:00:00.000000000Z", METRIC: "1500.5"},
    {"asset": "btc", "time": "2010-07-19T00:00:00.000000000Z", METRIC: "1200.0"},
    {"asset": "btc", "time": "2010-07-21T00:00:00.000000000Z"},
    {"asset": "btc", "time": "2010-07-22T00:00:00.000000000Z", METRIC: ""},
    {"asset": "btc", "time": "2010-07-23T00:00:00.000000000Z", METRIC: "oops"},
    {"asset": "btc", "time": "", METRIC: "9.9"},
]


class TestRowsFromMetric:
    def test_skips_missing_empty_and_nonnumeric(self) -> None:
        dates = [r["date"] for r in rows_from_metric(DATA, METRIC)]
        assert dates == [
            dt.date(2010, 7, 18),
            dt.date(2010, 7, 19),
            dt.date(2010, 7, 20),
        ]

    def test_rows_are_sorted_oldest_first(self) -> None:
        rows = rows_from_metric(DATA, METRIC)
        assert rows[0]["date"] < rows[1]["date"] < rows[2]["date"]

    def test_value_lands_in_close_only(self) -> None:
        first = rows_from_metric(DATA, METRIC)[0]
        assert first["close"] == 1000.0
        assert first["open"] is None
        assert first["high"] is None
        assert first["low"] is None
        assert first["volume"] is None

    def test_parses_nanosecond_timestamp_to_date(self) -> None:
        row = rows_from_metric(
            [{"time": "2021-11-08T00:00:00.000000000Z", METRIC: "1300000000000"}],
            METRIC,
        )[0]
        assert row["date"] == dt.date(2021, 11, 8)
        assert row["close"] == 1.3e12

    def test_reads_the_requested_metric_by_name(self) -> None:
        # A point that carries several metrics: only the requested one is read.
        rows = rows_from_metric(
            [{"time": "2020-01-01T00:00:00.000000000Z", "CapRealUSD": "50", METRIC: "80"}],
            "CapRealUSD",
        )
        assert rows[0]["close"] == 50.0

    def test_empty_input_yields_no_rows(self) -> None:
        assert rows_from_metric([], METRIC) == []
