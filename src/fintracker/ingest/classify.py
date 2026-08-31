"""Allocation buckets for instruments: sector and region.

The portfolio dashboard slices holdings by asset class, sector, region, and
currency. Asset class and currency are already on the instrument (`kind`,
`currency`); the other two come from here and land in `instruments.sector` /
`instruments.region`:

* **Equities** take Yahoo Finance's own `sector` and `country`, with the
  country folded into a continent-scale region so the pie has a readable
  number of slices.
* **Everything else** takes a fixed label from its kind — a Bitcoin holding
  has no Yahoo sector, but "Crypto / Global" is the honest bucket.

Rows already classified are left alone unless `refresh=True`, so one Yahoo
lookup per instrument is enough and a hand-edited sector survives the next run.

Run one classification pass by hand with:
    python -m fintracker.ingest.classify
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select

from fintracker.db import session_scope
from fintracker.models import Instrument

log = logging.getLogger(__name__)

# Country (as Yahoo spells it) → continent-scale region. Anything unlisted
# falls back to "Other", which stays visible on the pie rather than vanishing.
_COUNTRY_REGIONS: dict[str, str] = {
    "United States": "North America",
    "Canada": "North America",
    "Mexico": "North America",
    "Bermuda": "North America",
    "Argentina": "South America",
    "Brazil": "South America",
    "Chile": "South America",
    "Colombia": "South America",
    "Peru": "South America",
    "Austria": "Europe",
    "Belgium": "Europe",
    "Cyprus": "Europe",
    "Czechia": "Europe",
    "Czech Republic": "Europe",
    "Denmark": "Europe",
    "Estonia": "Europe",
    "Finland": "Europe",
    "France": "Europe",
    "Germany": "Europe",
    "Greece": "Europe",
    "Hungary": "Europe",
    "Iceland": "Europe",
    "Ireland": "Europe",
    "Italy": "Europe",
    "Jersey": "Europe",
    "Latvia": "Europe",
    "Lithuania": "Europe",
    "Luxembourg": "Europe",
    "Malta": "Europe",
    "Monaco": "Europe",
    "Netherlands": "Europe",
    "Norway": "Europe",
    "Poland": "Europe",
    "Portugal": "Europe",
    "Romania": "Europe",
    "Slovakia": "Europe",
    "Slovenia": "Europe",
    "Spain": "Europe",
    "Sweden": "Europe",
    "Switzerland": "Europe",
    "Ukraine": "Europe",
    "United Kingdom": "Europe",
    "China": "Asia",
    "Hong Kong": "Asia",
    "India": "Asia",
    "Indonesia": "Asia",
    "Israel": "Asia",
    "Japan": "Asia",
    "Malaysia": "Asia",
    "Philippines": "Asia",
    "Singapore": "Asia",
    "South Korea": "Asia",
    "Taiwan": "Asia",
    "Thailand": "Asia",
    "Turkey": "Asia",
    "United Arab Emirates": "Asia",
    "Vietnam": "Asia",
    "Egypt": "Africa",
    "Morocco": "Africa",
    "Nigeria": "Africa",
    "South Africa": "Africa",
    "Australia": "Oceania",
    "New Zealand": "Oceania",
}

# Non-equity kinds have no Yahoo sector; these are their buckets.
_KIND_BUCKETS: dict[str, tuple[str, str]] = {
    "crypto": ("Crypto", "Global"),
    "metal": ("Precious metals", "Global"),
    "index": ("Broad market", "Global"),
    "forex": ("Cash & FX", "Global"),
    "rate": ("Fixed income", "Global"),
    "onchain": ("Crypto", "Global"),
}

UNKNOWN_SECTOR = "Unclassified"
UNKNOWN_REGION = "Other"


def region_for_country(country: str | None) -> str:
    """Continent-scale region for a Yahoo country string."""
    if not country:
        return UNKNOWN_REGION
    return _COUNTRY_REGIONS.get(country.strip(), UNKNOWN_REGION)


def buckets_for_kind(kind: str) -> tuple[str, str] | None:
    """Fixed (sector, region) for a non-equity kind, or None for equities."""
    return _KIND_BUCKETS.get(kind)


def classify(kind: str, sector: str | None, country: str | None) -> tuple[str, str]:
    """The (sector, region) an instrument belongs in.

    Pure, so the mapping is unit-tested without touching Yahoo. Equities use
    the fetched sector/country; every other kind uses its fixed bucket and
    ignores whatever Yahoo may have said.
    """
    fixed = buckets_for_kind(kind)
    if fixed is not None:
        return fixed
    return (sector or "").strip() or UNKNOWN_SECTOR, region_for_country(country)


def _yahoo_profile(yahoo_symbol: str) -> tuple[str | None, str | None]:
    """(sector, country) from Yahoo, or (None, None) when it doesn't answer."""
    import yfinance as yf

    try:
        info = yf.Ticker(yahoo_symbol).info or {}
    except Exception:
        log.warning("Yahoo profile lookup failed for %s", yahoo_symbol, exc_info=True)
        return None, None
    return info.get("sector"), info.get("country")


def classify_instruments(refresh: bool = False) -> int:
    """Fill in sector/region for instruments that lack them.

    Returns how many rows were updated. Non-equities are labelled from their
    kind without a network call; equities that Yahoo can't place still get a
    row ("Unclassified / Other") so the next run doesn't retry them forever.
    """
    with session_scope() as session:
        query = select(Instrument)
        if not refresh:
            query = query.where(or_(Instrument.sector.is_(None), Instrument.region.is_(None)))
        instruments = session.scalars(query.order_by(Instrument.symbol)).all()

        updated = 0
        for instrument in instruments:
            sector = country = None
            if buckets_for_kind(instrument.kind) is None and instrument.yahoo_symbol:
                sector, country = _yahoo_profile(instrument.yahoo_symbol)
            instrument.sector, instrument.region = classify(instrument.kind, sector, country)
            updated += 1

    if updated:
        log.info("Classified %d instrument(s) into sector/region buckets.", updated)
    return updated


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    classify_instruments()
