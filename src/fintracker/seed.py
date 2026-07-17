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
    # Crypto (CoinGecko ids).
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "kind": "crypto",
        "currency": "USD",
        "coingecko_id": "bitcoin",
    },
    {
        "symbol": "ETH",
        "name": "Ethereum",
        "kind": "crypto",
        "currency": "USD",
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
