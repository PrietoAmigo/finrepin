"""Unit tests for the pure weekly-report helpers."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fintracker.models import Price
from fintracker.report.data import change_pct


def _price(day: str, close: str) -> Price:
    return Price(date=dt.date.fromisoformat(day), close=Decimal(close))


# Newest-first, like the report query returns them.
PRICES = [
    _price("2026-07-17", "110"),
    _price("2026-07-16", "108"),
    _price("2026-07-10", "100"),
    _price("2026-06-15", "88"),
    _price("2025-07-15", "55"),
]


class TestChangePct:
    def test_week_uses_first_bar_at_least_that_old(self) -> None:
        assert change_pct(PRICES, 7) == pytest.approx(10.0)  # 110 vs 100 on 2026-07-10

    def test_month(self) -> None:
        assert change_pct(PRICES, 30) == 25.0  # 110 vs 88 on 2026-06-15

    def test_year(self) -> None:
        assert change_pct(PRICES, 365) == 100.0  # 110 vs 55 on 2025-07-15

    def test_no_base_old_enough_returns_none(self) -> None:
        assert change_pct(PRICES[:2], 365) is None

    def test_empty_returns_none(self) -> None:
        assert change_pct([], 7) is None

    def test_zero_base_returns_none(self) -> None:
        prices = [_price("2026-07-17", "10"), _price("2026-07-01", "0")]
        assert change_pct(prices, 7) is None
