"""Daily market ingest orchestrator: equities + forex (Yahoo) and crypto."""

from __future__ import annotations

import logging

from fintracker.ingest.crypto import ingest_crypto_prices
from fintracker.ingest.forex import ingest_forex_rates
from fintracker.ingest.prices import ingest_equity_prices

log = logging.getLogger(__name__)


def ingest_market_data() -> None:
    """Run all market ingestors; one source failing must not stop the others."""
    totals: dict[str, int] = {}
    for name, ingestor in (
        ("equities", ingest_equity_prices),
        ("forex", ingest_forex_rates),
        ("crypto", ingest_crypto_prices),
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
