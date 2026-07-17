"""Next upcoming earnings date per equity, via yfinance's calendar."""

from __future__ import annotations

import datetime as dt
import logging

import yfinance as yf
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.db import session_scope
from fintracker.models import EarningsDate, Instrument

log = logging.getLogger(__name__)


def next_earnings_date(yahoo_symbol: str) -> tuple[dt.date, bool] | None:
    """Return (date, is_estimated) for the next earnings event, or None.

    Yahoo reports either a confirmed single date or an estimated window of
    candidate dates; a window means the date is an estimate.
    """
    calendar = yf.Ticker(yahoo_symbol).calendar
    if not isinstance(calendar, dict):
        return None
    raw_dates = calendar.get("Earnings Date") or []
    dates = sorted(d for d in raw_dates if isinstance(d, dt.date))
    today = dt.date.today()
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return None
    return upcoming[0], len(dates) > 1


def ingest_earnings_dates() -> int:
    """Upsert the next earnings date for each equity; skip names Yahoo doesn't cover."""
    stored = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "equity", Instrument.yahoo_symbol.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.yahoo_symbol is not None
            try:
                result = next_earnings_date(inst.yahoo_symbol)
            except Exception:
                log.exception("Earnings lookup failed for %s", inst.symbol)
                continue
            if result is None:
                log.info("No upcoming earnings date for %s — skipped.", inst.symbol)
                continue
            earnings_date, is_estimated = result
            stmt = pg_insert(EarningsDate).values(
                instrument_id=inst.id,
                earnings_date=earnings_date,
                is_estimated=is_estimated,
                source="yfinance",
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["instrument_id"],
                    set_={
                        "earnings_date": stmt.excluded.earnings_date,
                        "is_estimated": stmt.excluded.is_estimated,
                        "source": stmt.excluded.source,
                        "updated_at": func.now(),
                    },
                )
            )
            stored += 1
            log.info(
                "Next earnings for %s: %s%s",
                inst.symbol,
                earnings_date,
                " (estimated)" if is_estimated else "",
            )
    return stored
