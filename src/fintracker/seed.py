"""Instrument registry seed.

Edit INSTRUMENTS to change what is tracked; rows are upserted by symbol on
every boot, so additions appear after a restart. Runtime-resolved fields
(`cik`) are never overwritten by the seed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.db import session_scope
from fintracker.models import Instrument

log = logging.getLogger(__name__)


def _index(symbol: str, name: str, currency: str, yahoo_symbol: str | None) -> dict[str, Any]:
    """Compact constructor for market-index registry rows."""
    return {
        "symbol": symbol,
        "name": name,
        "kind": "index",
        "currency": currency,
        "yahoo_symbol": yahoo_symbol,
    }


INSTRUMENTS: list[dict[str, Any]] = [
    # Equities. `taxonomy` marks names with SEC XBRL coverage (us-gaap / ifrs-full);
    # leave it None for listings that don't file with the SEC.
    {
        "symbol": "UNH",
        "name": "UnitedHealth Group",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "UNH",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "PRM",
        "name": "Perimeter Solutions",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "PRM",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "BN",
        "name": "Brookfield Corporation",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "BN",
        "taxonomy": "ifrs-full",
    },
    {
        "symbol": "CSU.TO",
        "name": "Constellation Software",
        "kind": "equity",
        "currency": "CAD",
        "yahoo_symbol": "CSU.TO",
        "taxonomy": None,
    },
    {
        "symbol": "AI.PA",
        "name": "Air Liquide",
        "kind": "equity",
        "currency": "EUR",
        "yahoo_symbol": "AI.PA",
        "taxonomy": None,
    },
    {
        "symbol": "AMZN",
        "name": "Amazon.com",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "AMZN",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "BMI",
        "name": "Badger Meter",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "BMI",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "CSL",
        "name": "Carlisle Companies",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "CSL",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "RMS.PA",
        "name": "Hermès International",
        "kind": "equity",
        "currency": "EUR",
        "yahoo_symbol": "RMS.PA",
        "taxonomy": None,
    },
    {
        "symbol": "KNSL",
        "name": "Kinsale Capital Group",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "KNSL",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "KRI.AT",
        "name": "Kri-Kri Milk Industry",
        "kind": "equity",
        "currency": "EUR",
        "yahoo_symbol": "KRI.AT",
        "taxonomy": None,
    },
    {
        "symbol": "MKL",
        "name": "Markel Group",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "MKL",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "ROL",
        "name": "Rollins",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "ROL",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "MSTR",
        "name": "Strategy (Class A)",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "MSTR",
        "taxonomy": "us-gaap",
    },
    {
        "symbol": "TEL",
        "name": "TE Connectivity",
        "kind": "equity",
        "currency": "USD",
        "yahoo_symbol": "TEL",
        "taxonomy": "us-gaap",
    },
    # Global market indexes — one per continent (plus an emerging-markets
    # aggregate) to keep cross-correlations low on the Market Overview panel.
    _index("SPX", "S&P 500 (North America)", "USD", "^GSPC"),
    _index("STOXX600", "STOXX Europe 600 (Europe)", "EUR", "^STOXX"),
    _index("N225", "Nikkei 225 (Asia)", "JPY", "^N225"),
    _index("ASX200", "S&P/ASX 200 (Oceania)", "AUD", "^AXJO"),
    _index("BOVESPA", "Bovespa (South America)", "BRL", "^BVSP"),
    _index("EEM", "MSCI Emerging Markets (iShares ETF)", "USD", "EEM"),
    # European market indexes (one per country). `yahoo_symbol=None` marks
    # exchanges Yahoo Finance does not cover (Sarajevo, Sofia, Zagreb, Cyprus,
    # Malta, Montenegro, Skopje, Bucharest, Belgrade, Bratislava, Ljubljana,
    # Kyiv); they stay registered so a symbol can be filled in later, but the
    # ingest skips them and the dashboard simply shows no series.
    _index("ATX", "ATX (Austria)", "EUR", "^ATX"),
    _index("BEL20", "BEL 20 (Belgium)", "EUR", "^BFX"),
    _index("SASX10", "SASX-10 (Bosnia and Herzegovina)", "BAM", None),
    _index("SOFIX", "SOFIX (Bulgaria)", "BGN", None),
    _index("CROBEX", "CROBEX (Croatia)", "EUR", None),
    _index("CYSE20", "FTSE/CySE 20 (Cyprus)", "EUR", None),
    _index("PX", "PX Index (Czechia)", "CZK", "^PX"),
    _index("OMXC25", "OMX Copenhagen 25 (Denmark)", "DKK", "^OMXC25"),
    _index("OMXTGI", "OMX Tallinn GI (Estonia)", "EUR", "^OMXTGI"),
    _index("OMXH25", "OMX Helsinki 25 (Finland)", "EUR", "^OMXH25"),
    _index("CAC40", "CAC 40 (France)", "EUR", "^FCHI"),
    _index("DAX", "DAX (Germany)", "EUR", "^GDAXI"),
    _index("ATHEX", "ATHEX Composite (Greece)", "EUR", "GD.AT"),
    _index("BUX", "BUX (Hungary)", "HUF", "^BUX"),
    _index("OMXI15", "OMX Iceland 15 (Iceland)", "ISK", "^OMXI15"),
    _index("ISEQ", "ISEQ Overall (Ireland)", "EUR", "^ISEQ"),
    _index("FTSEMIB", "FTSE MIB (Italy)", "EUR", "FTSEMIB.MI"),
    _index("OMXRGI", "OMX Riga GI (Latvia)", "EUR", "^OMXRGI"),
    _index("OMXVGI", "OMX Vilnius GI (Lithuania)", "EUR", "^OMXVGI"),
    _index("LUXX", "LuxX (Luxembourg)", "EUR", "^LUXXX"),
    _index("MSE", "MSE Equity Total Return (Malta)", "EUR", None),
    _index("MNSE10", "MNSE10 (Montenegro)", "EUR", None),
    _index("AEX", "AEX (Netherlands)", "EUR", "^AEX"),
    _index("MBI10", "MBI10 (North Macedonia)", "MKD", None),
    _index("OSEBX", "OSEBX (Norway)", "NOK", "OSEBX.OL"),
    _index("WIG20", "WIG20 (Poland)", "PLN", "WIG20.WA"),
    _index("PSI", "PSI (Portugal)", "EUR", "PSI20.LS"),
    _index("BET", "BET (Romania)", "RON", None),
    _index("BELEX15", "BELEX15 (Serbia)", "RSD", None),
    _index("SAX", "SAX (Slovakia)", "EUR", None),
    _index("SBITOP", "SBITOP (Slovenia)", "EUR", None),
    _index("IBEX35", "IBEX 35 (Spain)", "EUR", "^IBEX"),
    _index("OMXS30", "OMX Stockholm 30 (Sweden)", "SEK", "^OMXS30"),
    _index("SMI", "SMI (Switzerland)", "CHF", "^SSMI"),
    _index("PFTS", "PFTS Index (Ukraine)", "UAH", None),
    _index("FTSE100", "FTSE 100 (United Kingdom)", "GBP", "^FTSE"),
    # Crypto: Yahoo for full daily OHLCV history, CoinGecko for the live spot.
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "kind": "crypto",
        "currency": "USD",
        "yahoo_symbol": "BTC-USD",
        "coingecko_id": "bitcoin",
    },
    {
        "symbol": "ETH",
        "name": "Ethereum",
        "kind": "crypto",
        "currency": "USD",
        "yahoo_symbol": "ETH-USD",
        "coingecko_id": "ethereum",
    },
    # Forex.
    {
        "symbol": "EUR/USD",
        "name": "Euro / US Dollar",
        "kind": "forex",
        "currency": "USD",
        "yahoo_symbol": "EURUSD=X",
    },
]


def seed_instruments() -> None:
    with session_scope() as session:
        for row in INSTRUMENTS:
            stmt = pg_insert(Instrument).values(**row)
            update_cols = {k: stmt.excluded[k] for k in row if k != "symbol"}
            session.execute(
                stmt.on_conflict_do_update(index_elements=["symbol"], set_=update_cols)
            )
    log.info("Seeded %d instruments.", len(INSTRUMENTS))
