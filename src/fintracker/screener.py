"""Keeps the Screener dashboard's fundamentals in step with the stored facts.

The Screener lists every equity at once. Its fundamentals figures come from
`screener_fundamentals` (migration 0037), a materialized view of the series
the statements give (TTM revenue, EPS, free cash flow, EBITDA, margins, book
equity, debt, ...), because computing them for every company on each
dashboard refresh takes seconds. Prices are read live, so the view only has to
follow the facts: it is refreshed after each fundamentals ingest (SEC and
Yahoo), after an on-demand ticker is ingested, and when the app starts, which
picks up any change a migration made to the views it is built from.

Refresh it by hand with:
    python -m fintracker.screener
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from fintracker.db import get_engine

log = logging.getLogger(__name__)

# CONCURRENTLY: the dashboard keeps reading the previous rows while the new
# ones are computed (a couple of seconds), instead of waiting on a lock. Two
# refreshes started together run one after the other.
REFRESH_SQL = "REFRESH MATERIALIZED VIEW CONCURRENTLY screener_fundamentals"


def refresh_screener() -> bool:
    """Recompute the Screener's fundamentals. Returns False if that failed.

    A failure is logged and leaves the previous figures in place; it never
    fails the ingest that asked for the refresh, whose facts are already
    committed.
    """
    try:
        with get_engine().begin() as conn:
            conn.execute(text(REFRESH_SQL))
    except Exception:
        log.exception("Screener refresh failed; it keeps the previous fundamentals until the next.")
        return False
    log.info("Screener fundamentals refreshed.")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(0 if refresh_screener() else 1)
