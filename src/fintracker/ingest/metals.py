"""Precious metals (gold, silver) — daily bars from Yahoo, like everything else.

The series are COMEX **continuous front-month futures**, `GC=F` and `SI=F`,
quoted in USD per troy ounce. They carry decades of daily history through the
same yfinance path as equities and indexes, so the ingest is a one-liner.

Front-month futures are not spot: cost of carry puts them roughly a percent
above the LBMA fix, and that basis is fairly stable — so the weekly, monthly
and yearly *moves* the report shows are effectively identical to spot's.

A free spot source was tried first and abandoned. Stooq's CSV download
(`xauusd`/`xagusd`) is genuinely key-less and carries decades of history, but
it now sits behind a JavaScript browser-verification wall: every request comes
back HTTP 200 with an HTML challenge page ("This site requires JavaScript to
verify your browser"), on both `stooq.com` and the `stooq.pl` mirror. Getting
past that needs a headless browser, which does not belong in a daily ingest.
The keyed APIs (goldapi.io, metals.dev) cap their free tier at ~100
requests/month with little history. If a free spot feed turns up later, the
instruments only need a different `yahoo_symbol` (Yahoo's own `XAUUSD=X`) or a
new client here — nothing else changes.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.metals
"""

from __future__ import annotations

import logging

from fintracker.ingest.prices import ingest_yahoo_prices

log = logging.getLogger(__name__)


def ingest_metal_prices() -> int:
    """Fetch + upsert daily bars for every `kind='metal'` instrument."""
    return ingest_yahoo_prices("metal")


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_metal_prices()
