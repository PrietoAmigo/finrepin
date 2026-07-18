"""Forex rates — same Yahoo daily-bar path as equities.

Besides the seeded EUR/USD, `ensure_fx_instruments` keeps one '<CCY>/USD'
pair registered for every currency that appears on an equity listing or a
fundamentals fact, so the `fx_usd_daily` view (migration 0007) can convert
dashboard values into any display currency. New pairs backfill their full
history on the next price fetch, exactly like any other instrument.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.db import session_scope
from fintracker.ingest.prices import ingest_yahoo_prices
from fintracker.models import Fundamental, Instrument

log = logging.getLogger(__name__)

_CCY_RE = re.compile(r"^[A-Z]{3}$")


def fx_instrument_rows(currencies: Iterable[str]) -> list[dict[str, Any]]:
    """One '<CCY>/USD' instrument row per valid non-USD ISO currency code.

    Inputs may be raw fundamentals units ('EUR/shares', 'shares'); anything
    that isn't a three-letter code after stripping the '/shares' suffix is
    ignored.
    """
    codes = {str(c).strip().upper().split("/", 1)[0] for c in currencies if c}
    return [
        {
            "symbol": f"{ccy}/USD",
            "name": f"{ccy} / US Dollar",
            "kind": "forex",
            "currency": "USD",
            "yahoo_symbol": f"{ccy}USD=X",
        }
        for ccy in sorted(codes - {"USD"})
        if _CCY_RE.fullmatch(ccy)
    ]


def ensure_fx_instruments() -> int:
    """Register missing '<CCY>/USD' pairs; returns how many were added."""
    added = 0
    with session_scope() as session:
        currencies = set(
            session.scalars(
                select(Instrument.currency).where(Instrument.kind == "equity").distinct()
            )
        )
        currencies |= set(session.scalars(select(Fundamental.unit).distinct()))
        existing = set(session.scalars(select(Instrument.symbol).where(Instrument.kind == "forex")))
        for row in fx_instrument_rows(currencies):
            if row["symbol"] in existing:
                continue
            session.execute(
                pg_insert(Instrument)
                .values(**row)
                .on_conflict_do_nothing(index_elements=["symbol"])
            )
            added += 1
    if added:
        log.info("Registered %d new FX pair(s).", added)
    return added


def ingest_forex_rates() -> int:
    ensure_fx_instruments()
    return ingest_yahoo_prices("forex")
