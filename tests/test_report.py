"""Unit tests for the pure weekly-report helpers."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fintracker.models import Price
from fintracker.report.data import PriceRow, Report, change_abs, change_pct
from fintracker.report.render import render_html, render_text


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


# Newest-first (date, value) points, like the MVRV Z-Score query returns them.
POINTS = [
    (dt.date(2026, 7, 17), 3.10),
    (dt.date(2026, 7, 16), 2.90),
    (dt.date(2026, 7, 10), 2.00),
    (dt.date(2026, 6, 15), 1.20),
    (dt.date(2025, 7, 15), -0.50),
]


class TestChangeAbs:
    def test_week_uses_first_point_at_least_that_old(self) -> None:
        assert change_abs(POINTS, 7) == pytest.approx(1.10)  # 3.10 - 2.00

    def test_month(self) -> None:
        assert change_abs(POINTS, 30) == pytest.approx(1.90)  # 3.10 - 1.20

    def test_year_can_be_negative_base(self) -> None:
        assert change_abs(POINTS, 365) == pytest.approx(3.60)  # 3.10 - (-0.50)

    def test_no_base_old_enough_returns_none(self) -> None:
        assert change_abs(POINTS[:2], 365) is None

    def test_empty_returns_none(self) -> None:
        assert change_abs([], 7) is None


def _report_with_score() -> Report:
    return Report(
        generated_at=dt.date(2026, 7, 13),
        lookback_days=7,
        prices=[
            PriceRow("BTC", "Bitcoin", "crypto", "USD", 64000.0, 2.1, 6.4, 43.8),
            PriceRow(
                "MVRV Z-Score", "Bitcoin · market cap vs realized cap",
                "crypto", "", 2.35, 0.18, -0.42, 1.90, is_score=True,
            ),
        ],
    )


class TestScoreRendering:
    def test_html_score_level_has_no_currency_symbol(self) -> None:
        html = render_html(_report_with_score())
        assert ">2.35<" in html  # plain number, no "$"
        assert "$2.35" not in html

    def test_html_score_moves_are_deltas_not_percentages(self) -> None:
        html = render_html(_report_with_score())
        assert ">+0.18<" in html and ">-0.42<" in html
        assert "+0.18%" not in html  # a score move is an absolute delta

    def test_html_section_header_uses_accent(self) -> None:
        # The Crypto/Stocks/Forex section headers stand out in the accent color.
        assert "#1967d2" in render_html(_report_with_score())

    def test_text_score_level_and_moves(self) -> None:
        text = render_text(_report_with_score())
        assert "=== CRYPTO ===" in text
        assert "2.35" in text and "+0.18" in text
        assert "+0.18%" not in text

    def test_eth_absence_is_a_data_choice_not_rendering(self) -> None:
        # Rendering shows whatever rows it's given; ETH exclusion lives in data.py.
        assert "ETH" not in render_html(_report_with_score())
