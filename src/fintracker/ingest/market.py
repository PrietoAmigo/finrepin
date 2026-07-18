"""Daily market ingest orchestrator: equities, indexes, forex, and crypto.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.market
"""

from __future__ import annotations

import logging

from fintracker.ingest.crypto import ingest_crypto_history, ingest_crypto_prices
from fintracker.ingest.ecb import ingest_ecb_rates
from fintracker.ingest.forex import ingest_forex_rates
from fintracker.ingest.fred import ingest_interest_rates
from fintracker.ingest.prices import ingest_equity_prices, ingest_index_prices

log = logging.getLogger(__name__)


def ingest_market_data() -> None:
    """Run all market ingestors; one source failing must not stop the others."""
    totals: dict[str, int] = {}
    for name, ingestor in (
        ("equities", ingest_equity_prices),
        ("indexes", ingest_index_prices),
        ("forex", ingest_forex_rates),
        ("interest-rates", ingest_interest_rates),
        ("interest-rates-ecb", ingest_ecb_rates),
        ("crypto-history", ingest_crypto_history),
        ("crypto-spot", ingest_crypto_prices),
    ):
        try:
            totals[name] = ingestor()
        except Exception:
            log.exception("Market ingest step %r failed", name)
            totals[name] = 0
    log.info(
        "Market ingest done: %s",
        ", ".join(f"{name}={count} rows" for name, count in totals.items()),
    )


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_market_data()
