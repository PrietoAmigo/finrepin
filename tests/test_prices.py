"""Unit tests for the pure price-ingest helpers — no network, no database."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from fintracker.ingest.prices import incremental_start, rows_from_history

TODAY = dt.date(2026, 7, 17)


class TestIncrementalStart:
    def test_no_stored_rows_triggers_full_backfill(self) -> None:
        assert incremental_start(None, None, TODAY) is None

    def test_only_recent_bootstrap_rows_triggers_full_backfill(self) -> None:
        # A pre-backfill database holds just a few recent days.
        earliest = TODAY - dt.timedelta(days=7)
        assert incremental_start(earliest, TODAY, TODAY) is None

    def test_deep_history_fetches_incrementally_with_overlap(self) -> None:
        earliest = dt.date(2010, 1, 4)
        latest = TODAY - dt.timedelta(days=1)
        assert incremental_start(earliest, latest, TODAY) == latest - dt.timedelta(days=5)

    def test_threshold_boundary_is_treated_as_deep_history(self) -> None:
        earliest = TODAY - dt.timedelta(days=30)
        assert incremental_start(earliest, TODAY, TODAY) == TODAY - dt.timedelta(days=5)


class TestRowsFromHistory:
    def _frame(self) -> pd.DataFrame:
        idx = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-07-15", tz="America/New_York"),
                pd.Timestamp("2026-07-16", tz="America/New_York"),
                pd.Timestamp("2026-07-17", tz="America/New_York"),
            ]
        )
        return pd.DataFrame(
            {
                "Open": [100.0, float("nan"), 103.0],
                "High": [101.0, 103.0, 104.0],
                "Low": [99.0, 100.5, 102.0],
                "Close": [100.5, 102.0, float("nan")],
                "Volume": [1_000_000, float("nan"), 500_000],
            },
            index=idx,
        )

    def test_rows_are_sorted_and_nan_close_rows_are_dropped(self) -> None:
        rows = rows_from_history(self._frame())
        assert [r["date"] for r in rows] == [dt.date(2026, 7, 15), dt.date(2026, 7, 16)]

    def test_nan_open_and_volume_become_none(self) -> None:
        rows = rows_from_history(self._frame())
        assert rows[1]["open"] is None
        assert rows[1]["volume"] is None
        assert rows[1]["close"] == 102.0

    def test_volume_is_integer(self) -> None:
        rows = rows_from_history(self._frame())
        assert rows[0]["volume"] == 1_000_000
