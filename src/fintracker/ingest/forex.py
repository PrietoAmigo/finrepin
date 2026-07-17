"""Forex rates (EUR/USD) — same Yahoo daily-bar path as equities."""

from __future__ import annotations

from fintracker.ingest.prices import ingest_yahoo_prices


def ingest_forex_rates() -> int:
    return ingest_yahoo_prices("forex")
