"""Registry checks for the precious metals — no network, no database.

The ingest itself is the shared Yahoo daily-bar path (covered by
`tests/test_prices.py`), so what is worth pinning here is the registry: the
metals must stay a distinct kind with a Yahoo symbol, or they silently drop out
of both the ingest and the weekly email's Precious metals section.
"""

from __future__ import annotations

from fintracker.report.data import _KIND_UNITS
from fintracker.report.render import _KIND_HEADERS
from fintracker.seed import INSTRUMENTS

METALS = [row for row in INSTRUMENTS if row["kind"] == "metal"]


class TestMetalRegistry:
    def test_gold_and_silver_are_registered(self) -> None:
        assert {row["symbol"] for row in METALS} == {"XAU", "XAG"}

    def test_each_metal_has_a_yahoo_symbol(self) -> None:
        # ingest_yahoo_prices('metal') skips any instrument without one.
        assert all(row["yahoo_symbol"] for row in METALS)

    def test_metals_are_quoted_in_usd(self) -> None:
        assert {row["currency"] for row in METALS} == {"USD"}

    def test_names_do_not_promise_spot(self) -> None:
        # The rows are front-month futures, ~1% off spot; the email shows these
        # names, so they must not claim to be spot prices.
        assert all("spot" not in row["name"].lower() for row in METALS)


class TestMetalsReachTheReport:
    def test_the_report_renders_a_metal_section(self) -> None:
        assert "metal" in dict(_KIND_HEADERS)

    def test_metal_levels_are_quoted_per_troy_ounce(self) -> None:
        assert _KIND_UNITS["metal"] == "/oz"
