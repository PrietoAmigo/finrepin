"""Unit tests for the pure FX-pair derivation — no network, no database."""

from __future__ import annotations

from fintracker.ingest.forex import fx_instrument_rows


class TestFxInstrumentRows:
    def test_one_pair_per_non_usd_currency(self) -> None:
        rows = fx_instrument_rows({"USD", "CAD", "EUR"})
        assert [r["symbol"] for r in rows] == ["CAD/USD", "EUR/USD"]
        (cad,) = [r for r in rows if r["symbol"] == "CAD/USD"]
        assert cad == {
            "symbol": "CAD/USD",
            "name": "CAD / US Dollar",
            "kind": "forex",
            "currency": "USD",
            "yahoo_symbol": "CADUSD=X",
        }

    def test_fundamentals_units_are_normalized(self) -> None:
        # Raw fundamentals units carry '/shares' suffixes and non-currency units.
        rows = fx_instrument_rows({"EUR/shares", "shares", "USD/shares", "CAD"})
        assert [r["symbol"] for r in rows] == ["CAD/USD", "EUR/USD"]

    def test_non_iso_codes_and_empties_are_ignored(self) -> None:
        assert fx_instrument_rows({"", "1X", "TOOLONG", "usd"}) == []

    def test_lowercase_codes_are_uppercased(self) -> None:
        (row,) = fx_instrument_rows({"cad"})
        assert row["symbol"] == "CAD/USD"
