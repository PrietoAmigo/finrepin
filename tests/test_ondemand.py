"""Unit tests for the pure on-demand ticker-request helpers."""

from __future__ import annotations

from fintracker.ingest.ondemand import detect_taxonomy, normalize_symbol


class TestNormalizeSymbol:
    def test_uppercases_and_trims(self) -> None:
        assert normalize_symbol("  nvda ") == "NVDA"

    def test_accepts_exchange_suffixes(self) -> None:
        assert normalize_symbol("csu.to") == "CSU.TO"
        assert normalize_symbol("BRK-B") == "BRK-B"
        assert normalize_symbol("EURUSD=X") == "EURUSD=X"

    def test_rejects_empty_and_garbage(self) -> None:
        assert normalize_symbol("") is None
        assert normalize_symbol("   ") is None
        assert normalize_symbol("no spaces") is None
        assert normalize_symbol("'; DROP TABLE instruments; --") is None

    def test_rejects_leading_separator_and_overlong(self) -> None:
        assert normalize_symbol(".TO") is None
        assert normalize_symbol("A" * 33) is None


class TestDetectTaxonomy:
    def test_us_gaap(self) -> None:
        assert detect_taxonomy({"facts": {"us-gaap": {"Revenues": {}}}}) == "us-gaap"

    def test_ifrs(self) -> None:
        assert detect_taxonomy({"facts": {"ifrs-full": {"Revenue": {}}}}) == "ifrs-full"

    def test_prefers_us_gaap_when_both(self) -> None:
        facts = {"facts": {"ifrs-full": {"Revenue": {}}, "us-gaap": {"Revenues": {}}}}
        assert detect_taxonomy(facts) == "us-gaap"

    def test_neither_or_empty(self) -> None:
        assert detect_taxonomy({"facts": {"dei": {"x": {}}}}) is None
        assert detect_taxonomy({}) is None
        assert detect_taxonomy({"facts": {"us-gaap": {}}}) is None
