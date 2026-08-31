"""Unit tests for the on-demand ticker-request helpers."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from fintracker.ingest import ondemand
from fintracker.ingest.ondemand import (
    YahooMeta,
    detect_crypto,
    detect_crypto_pair,
    detect_taxonomy,
    match_coingecko_id,
    normalize_symbol,
)
from fintracker.models import Base, Instrument, TickerRequest


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


_COINS = [
    {"id": "monero", "symbol": "xmr", "name": "Monero"},
    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
    {"id": "solana", "symbol": "sol", "name": "Solana"},
    {"id": "wrapped-solana", "symbol": "sol", "name": "Wrapped Solana"},
]


class TestMatchCoingeckoId:
    def test_unique_symbol(self) -> None:
        assert match_coingecko_id("XMR", "Monero", _COINS) == "monero"

    def test_no_match(self) -> None:
        assert match_coingecko_id("ZZZ", "Nothing", _COINS) is None

    def test_ambiguous_resolved_by_name(self) -> None:
        assert match_coingecko_id("SOL", "Solana", _COINS) == "solana"
        assert match_coingecko_id("SOL", "Wrapped Solana", _COINS) == "wrapped-solana"

    def test_ambiguous_without_name_is_none(self) -> None:
        assert match_coingecko_id("SOL", "Unknown Coin", _COINS) is None


class TestPureCryptoHelpers:
    def test_is_bare_symbol(self) -> None:
        assert ondemand._is_bare_symbol("XMR")
        assert not ondemand._is_bare_symbol("BTC-USD")
        assert not ondemand._is_bare_symbol("CSU.TO")
        assert not ondemand._is_bare_symbol("EURUSD=X")

    def test_strip_usd(self) -> None:
        assert ondemand._strip_usd("XMR-USD") == "XMR"
        assert ondemand._strip_usd("XMR") == "XMR"

    def test_clean_crypto_name(self) -> None:
        assert ondemand._clean_crypto_name("Monero USD") == "Monero"
        assert ondemand._clean_crypto_name("Bitcoin") == "Bitcoin"


def _crypto_meta(name: str = "Monero USD") -> YahooMeta:
    return YahooMeta(quote_type="CRYPTOCURRENCY", name=name, currency="USD")


def _equity_meta() -> YahooMeta:
    return YahooMeta(quote_type="EQUITY", name="NVIDIA Corporation", currency="USD")


class TestDetectCrypto:
    def test_bare_ticker_probes_usd_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ondemand, "_resolve_coingecko_id", lambda s, n: "monero")
        monkeypatch.setattr(
            ondemand, "_yahoo_meta", lambda sym: _crypto_meta() if sym == "XMR-USD" else None
        )
        spec = detect_crypto("XMR")
        assert spec is not None
        assert (spec.symbol, spec.yahoo_symbol, spec.name, spec.coingecko_id) == (
            "XMR", "XMR-USD", "Monero", "monero",
        )

    def test_usd_pair_taken_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ondemand, "_resolve_coingecko_id", lambda s, n: "bitcoin")
        monkeypatch.setattr(
            ondemand, "_yahoo_meta",
            lambda sym: _crypto_meta("Bitcoin USD") if sym == "BTC-USD" else None,
        )
        spec = detect_crypto("BTC-USD")
        assert spec is not None and spec.symbol == "BTC" and spec.yahoo_symbol == "BTC-USD"

    def test_equity_is_not_crypto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ondemand, "_yahoo_meta", lambda sym: _equity_meta())
        assert detect_crypto("NVDA") is None

    def test_equity_ticker_not_probed_as_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A ticker Yahoo knows as an equity must not be misclassified via <SYM>-USD.
        monkeypatch.setattr(
            ondemand, "_yahoo_meta",
            lambda sym: _equity_meta() if sym == "SOL" else _crypto_meta("Solana USD"),
        )
        assert detect_crypto("SOL") is None

    def test_unknown_symbol_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ondemand, "_yahoo_meta", lambda sym: None)
        assert detect_crypto("ZZZZ") is None


class TestDetectCryptoPair:
    """The `<SYM>-USD` probe on its own — the equity path's fallback."""

    def test_accepts_a_coin_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ondemand, "_resolve_coingecko_id", lambda s, n: "monero")
        monkeypatch.setattr(
            ondemand, "_yahoo_meta", lambda sym: _crypto_meta() if sym == "XMR-USD" else None
        )
        spec = detect_crypto_pair("XMR")
        assert spec is not None and spec.symbol == "XMR" and spec.yahoo_symbol == "XMR-USD"

    def test_rejects_a_pair_yahoo_does_not_call_crypto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ondemand, "_yahoo_meta", lambda sym: _equity_meta())
        assert detect_crypto_pair("NVDA") is None

    def test_skips_symbols_that_are_not_bare_tickers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No probe for exchange-suffixed symbols — CSU.TO-USD is not a thing.
        called: list[str] = []
        monkeypatch.setattr(
            ondemand, "_yahoo_meta", lambda sym: called.append(sym) or _crypto_meta()
        )
        assert detect_crypto_pair("CSU.TO") is None
        assert called == []


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _history() -> pd.DataFrame:
    idx = pd.to_datetime(["2026-08-19", "2026-08-20"])
    return pd.DataFrame(
        {"Open": [100.0, 110.0], "High": [101.0, 111.0], "Low": [99.0, 109.0],
         "Close": [100.0, 110.0], "Volume": [10, 20]},
        index=idx,
    )


class TestResolveCrypto:
    def test_resolve_registers_crypto(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ondemand, "_yahoo_meta", lambda sym: _crypto_meta() if sym == "XMR-USD" else None
        )
        monkeypatch.setattr(ondemand, "_resolve_coingecko_id", lambda s, n: "monero")
        monkeypatch.setattr(ondemand, "fetch_daily_history", lambda ys, start=None: _history())
        # upsert_price_rows uses the Postgres insert dialect, so stub it for SQLite.
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            ondemand, "upsert_price_rows",
            lambda s, iid, rows, source: captured.update(rows=rows, source=source) or len(rows),
        )

        req = TickerRequest(symbol="XMR")
        session.add(req)
        session.flush()

        status, note = ondemand._resolve(req, session)
        assert status == "done"
        assert "crypto" in note
        inst = session.scalar(select(Instrument).where(Instrument.symbol == "XMR"))
        assert inst is not None
        assert (inst.kind, inst.yahoo_symbol, inst.coingecko_id, inst.currency) == (
            "crypto", "XMR-USD", "monero", "USD",
        )
        assert captured["source"] == "yfinance"
        assert len(captured["rows"]) == 2  # type: ignore[arg-type]


class TestResolveCryptoFallback:
    """A bare coin ticker that Yahoo answers for as something else.

    `detect_crypto` deliberately leaves such a symbol to the equity path so a
    real listing always wins. When that path then finds nothing at all, the
    request used to dead-end at `not_found` — the reason XMR could not be
    added. `_resolve` now probes `<SYM>-USD` before giving up.
    """

    @staticmethod
    def _meta_for(sym: str) -> YahooMeta | None:
        # Yahoo answers for the bare ticker (some unrelated listing) *and* for
        # the coin pair — the case the old code could not get past.
        if sym == "XMR":
            return _equity_meta()
        if sym == "XMR-USD":
            return _crypto_meta()
        return None

    @pytest.fixture(autouse=True)
    def _no_sec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No SEC_USER_AGENT configured -> the EDGAR lookup is skipped entirely.
        monkeypatch.setattr(ondemand, "get_settings", lambda: SimpleNamespace(sec_user_agent=""))

    def test_registers_the_coin_when_the_equity_path_is_empty(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ondemand, "_yahoo_meta", self._meta_for)
        monkeypatch.setattr(ondemand, "_resolve_coingecko_id", lambda s, n: "monero")
        # Nothing as an equity; the coin pair has history.
        monkeypatch.setattr(
            ondemand,
            "fetch_daily_history",
            lambda ys, start=None: _history() if ys == "XMR-USD" else pd.DataFrame(),
        )
        monkeypatch.setattr(
            ondemand, "upsert_price_rows", lambda s, iid, rows, source: len(rows)
        )

        req = TickerRequest(symbol="XMR")
        session.add(req)
        session.flush()

        status, note = ondemand._resolve(req, session)
        assert status == "done"
        assert "crypto" in note
        inst = session.scalar(select(Instrument).where(Instrument.symbol == "XMR"))
        assert inst is not None
        assert (inst.kind, inst.yahoo_symbol, inst.coingecko_id) == (
            "crypto", "XMR-USD", "monero",
        )

    def test_still_not_found_when_the_pair_is_not_a_coin_either(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ondemand, "_yahoo_meta", lambda sym: None)
        monkeypatch.setattr(ondemand, "fetch_daily_history", lambda ys, start=None: pd.DataFrame())

        req = TickerRequest(symbol="ZZZZ")
        session.add(req)
        session.flush()

        status, note = ondemand._resolve(req, session)
        assert status == "not_found"
        assert "unknown to Yahoo Finance" in note

    def test_a_real_equity_still_wins_over_a_same_named_coin(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SOL is an equity on Yahoo *and* has a SOL-USD coin pair. The equity
        # has price history, so the fallback must never be reached.
        monkeypatch.setattr(
            ondemand,
            "_yahoo_meta",
            lambda sym: _equity_meta() if sym == "SOL" else _crypto_meta("Solana USD"),
        )
        monkeypatch.setattr(ondemand, "fetch_daily_history", lambda ys, start=None: _history())
        monkeypatch.setattr(
            ondemand, "upsert_price_rows", lambda s, iid, rows, source: len(rows)
        )
        monkeypatch.setattr(ondemand, "_fetch_currency", lambda sym: "USD")

        req = TickerRequest(symbol="SOL")
        session.add(req)
        session.flush()

        status, _ = ondemand._resolve(req, session)
        assert status == "done"
        inst = session.scalar(select(Instrument).where(Instrument.symbol == "SOL"))
        assert inst is not None and inst.kind == "equity"
