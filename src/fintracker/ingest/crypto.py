"""Crypto spot prices from CoinGecko's free, key-less simple/price endpoint.

The endpoint returns the current price only, so crypto rows carry just
`close` (upserted onto today's date in the configured timezone).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.ingest.prices import ingest_yahoo_prices, upsert_price_rows
from fintracker.models import Instrument

log = logging.getLogger(__name__)

COINGECKO_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def fetch_simple_prices(coin_ids: list[str], vs_currency: str = "usd") -> dict[str, Any]:
    resp = requests.get(
        COINGECKO_SIMPLE_PRICE,
        params={"ids": ",".join(coin_ids), "vs_currencies": vs_currency},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _today() -> dt.date:
    try:
        tz = ZoneInfo(get_settings().tz)
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.datetime.now(tz).date()


def ingest_crypto_history() -> int:
    """Full-history backfill + incremental daily bars via Yahoo (BTC-USD, ETH-USD)."""
    return ingest_yahoo_prices("crypto")


def ingest_crypto_prices() -> int:
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "crypto", Instrument.coingecko_id.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        if not instruments:
            return 0
        ids = [inst.coingecko_id for inst in instruments if inst.coingecko_id]
        try:
            data = fetch_simple_prices(ids)
        except Exception:
            log.exception("CoinGecko fetch failed for %s", ids)
            return 0
        today = _today()
        for inst in instruments:
            quote = data.get(inst.coingecko_id or "", {})
            price = quote.get("usd")
            if price is None:
                log.warning("No CoinGecko quote for %s (%s)", inst.symbol, inst.coingecko_id)
                continue
            row = {"date": today, "open": None, "high": None, "low": None,
                   "close": float(price), "volume": None}
            total += upsert_price_rows(session, inst.id, [row], source="coingecko")
            log.info("Upserted spot price for %s: %s USD", inst.symbol, price)
    return total
