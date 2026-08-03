"""Unit tests for the precious-metals ingest — no network, no database."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import requests

from fintracker.ingest.metals import _is_retryable, metal_rows, rows_from_stooq_csv
from fintracker.models import Instrument

# A Stooq daily CSV: header, an out-of-order row to prove sorting, a row with
# no volume column value, and one with an unusable close.
SAMPLE = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-07-17,3340.10,3362.55,3335.00,3358.90,120345\n"
    "2026-07-15,3300.00,3312.40,3288.10,3305.75,98211\n"
    "2026-07-16,3358.00,3360.00,3339.90,N/A,0\n"
)


class TestRowsFromStooqCsv:
    def test_rows_are_sorted_oldest_first(self) -> None:
        assert [r["date"] for r in rows_from_stooq_csv(SAMPLE)] == [
            dt.date(2026, 7, 15),
            dt.date(2026, 7, 17),
        ]

    def test_ohlcv_is_mapped_by_header_name(self) -> None:
        first = rows_from_stooq_csv(SAMPLE)[0]
        assert first["open"] == 3300.00
        assert first["high"] == 3312.40
        assert first["low"] == 3288.10
        assert first["close"] == 3305.75
        assert first["volume"] == 98211

    def test_unusable_close_is_skipped(self) -> None:
        assert dt.date(2026, 7, 16) not in {r["date"] for r in rows_from_stooq_csv(SAMPLE)}

    def test_missing_volume_column_is_tolerated(self) -> None:
        rows = rows_from_stooq_csv("Date,Open,High,Low,Close\n2026-07-17,1,2,0.5,1.5\n")
        assert rows[0]["close"] == 1.5
        assert rows[0]["volume"] is None

    def test_rate_limit_message_yields_no_rows(self) -> None:
        # Stooq answers throttled requests with plain text and HTTP 200; the
        # ingest reads "no rows" as its cue to fall back to the other source.
        assert rows_from_stooq_csv("Exceeded the daily hits limit\n") == []

    def test_empty_input_yields_no_rows(self) -> None:
        assert rows_from_stooq_csv("") == []
        assert rows_from_stooq_csv("Date,Open,High,Low,Close,Volume\n") == []


def _gold() -> Instrument:
    return Instrument(
        symbol="XAU",
        name="Gold (spot, per troy ounce)",
        kind="metal",
        currency="USD",
        stooq_symbol="xauusd",
        yahoo_symbol="GC=F",
    )


def _yahoo_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [3350.0], "High": [3370.0], "Low": [3340.0], "Close": [3365.0], "Volume": [42]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-17")]),
    )


class TestMetalRows:
    def test_stooq_is_preferred(self) -> None:
        rows, source = metal_rows(
            _gold(),
            None,
            fetch_stooq=lambda *a, **k: SAMPLE,
            fetch_yahoo=lambda *a, **k: _yahoo_frame(),
        )
        assert source == "stooq"
        assert rows[-1]["close"] == 3358.90

    def test_falls_back_to_yahoo_when_stooq_is_throttled(self) -> None:
        rows, source = metal_rows(
            _gold(),
            None,
            fetch_stooq=lambda *a, **k: "Exceeded the daily hits limit\n",
            fetch_yahoo=lambda *a, **k: _yahoo_frame(),
        )
        assert source == "yfinance"
        assert rows[0]["close"] == 3365.0

    def test_falls_back_to_yahoo_when_stooq_raises(self) -> None:
        def boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("connection reset")

        _, source = metal_rows(
            _gold(), None, fetch_stooq=boom, fetch_yahoo=lambda *a, **k: _yahoo_frame()
        )
        assert source == "yfinance"

    def test_http_error_falls_back_without_a_traceback(self) -> None:
        # Stooq answers some networks with 404 for symbols it serves fine in a
        # browser; that is an expected outcome, not a crash.
        def not_found(*args: object, **kwargs: object) -> str:
            raise requests.HTTPError("404 Client Error: Not Found")

        _, source = metal_rows(
            _gold(), None, fetch_stooq=not_found, fetch_yahoo=lambda *a, **k: _yahoo_frame()
        )
        assert source == "yfinance"

    def test_no_source_yields_no_rows(self) -> None:
        def boom(*args: object, **kwargs: object) -> pd.DataFrame:
            raise RuntimeError("delisted")

        rows, source = metal_rows(
            _gold(), None, fetch_stooq=lambda *a, **k: "", fetch_yahoo=boom
        )
        assert rows == []
        assert source == ""


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


class TestRetryPredicate:
    def test_4xx_is_not_retried(self) -> None:
        # A 404/403 is a settled answer: retrying it stalls the whole market
        # ingest before the Yahoo fallback gets its turn.
        assert not _is_retryable(_http_error(404))
        assert not _is_retryable(_http_error(403))

    def test_5xx_is_retried(self) -> None:
        assert _is_retryable(_http_error(503))

    def test_transport_errors_are_retried(self) -> None:
        assert _is_retryable(requests.ConnectionError("reset by peer"))
        assert _is_retryable(requests.Timeout("read timed out"))

    def test_unrelated_exceptions_are_not_retried(self) -> None:
        assert not _is_retryable(ValueError("nope"))
