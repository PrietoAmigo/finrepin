"""Daily OHLCV bars for Yahoo-covered instruments (equities, forex, crypto).

Fetches are state-aware: the first time an instrument has no Yahoo-sourced
rows, its entire available history is backfilled (period="max"); afterwards
each run fetches incrementally from a few days before the latest stored bar,
so weekends, holidays, and missed runs self-heal.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd
import yfinance as yf
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Instrument, Price

log = logging.getLogger(__name__)

# Re-fetch a few days before the latest stored bar so gaps self-heal.
OVERLAP_DAYS = 5
# If the earliest stored bar is this recent, assume only a bootstrap window
# exists (pre-backfill databases) and fetch the full history once.
BACKFILL_THRESHOLD_DAYS = 30

_UPSERT_CHUNK_SIZE = 500


def incremental_start(
    earliest: dt.date | None,
    latest: dt.date | None,
    today: dt.date,
    overlap_days: int = OVERLAP_DAYS,
    backfill_threshold_days: int = BACKFILL_THRESHOLD_DAYS,
) -> dt.date | None:
    """Start date for an incremental fetch, or None to fetch the full history.

    Pure so it can be unit-tested: `earliest`/`latest` are the bounds of the
    Yahoo-sourced rows already stored for the instrument.
    """
    if earliest is None or latest is None:
        return None
    if earliest > today - dt.timedelta(days=backfill_threshold_days):
        return None
    return latest - dt.timedelta(days=overlap_days)


def fetch_daily_history(yahoo_symbol: str, start: dt.date | None = None) -> pd.DataFrame:
    ticker = yf.Ticker(yahoo_symbol)
    if start is not None:
        return ticker.history(start=start, interval="1d", auto_adjust=False)
    return ticker.history(period="max", interval="1d", auto_adjust=False)


def rows_from_history(history: pd.DataFrame) -> list[dict[str, Any]]:
    """Normalize a yfinance history frame into upsertable dicts, latest last."""
    rows: dict[Any, dict[str, Any]] = {}
    for ts, bar in history.iterrows():
        close = bar.get("Close")
        if close is None or pd.isna(close):
            continue

        def _num(value: Any) -> float | None:
            return None if value is None or pd.isna(value) else float(value)

        volume = bar.get("Volume")
        date = ts.date()
        rows[date] = {
            "date": date,
            "open": _num(bar.get("Open")),
            "high": _num(bar.get("High")),
            "low": _num(bar.get("Low")),
            "close": float(close),
            "volume": None if volume is None or pd.isna(volume) else int(volume),
        }
    return [rows[k] for k in sorted(rows)]


def upsert_price_rows(
    session: Session, instrument_id: int, rows: list[dict[str, Any]], source: str
) -> int:
    """Chunked multi-row upserts so full-history backfills stay fast."""
    for offset in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        chunk = rows[offset : offset + _UPSERT_CHUNK_SIZE]
        stmt = pg_insert(Price).values(
            [{**row, "instrument_id": instrument_id, "source": source} for row in chunk]
        )
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_prices_instrument_date",
                set_={
                    col: getattr(stmt.excluded, col)
                    for col in ("open", "high", "low", "close", "volume", "source")
                },
            )
        )
    return len(rows)


def _stored_bounds(session: Session, instrument_id: int) -> tuple[dt.date | None, dt.date | None]:
    """(earliest, latest) date of Yahoo-sourced rows; other sources (e.g. the
    CoinGecko spot for today) must not suppress a pending backfill."""
    earliest, latest = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.instrument_id == instrument_id, Price.source == "yfinance"
        )
    ).one()
    return earliest, latest


def ingest_yahoo_prices(kind: str) -> int:
    """Fetch + upsert daily bars for every instrument of `kind` with a Yahoo symbol."""
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == kind, Instrument.yahoo_symbol.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.yahoo_symbol is not None
            earliest, latest = _stored_bounds(session, inst.id)
            start = incremental_start(earliest, latest, dt.date.today())
            try:
                history = fetch_daily_history(inst.yahoo_symbol, start=start)
            except Exception:
                log.exception("Price fetch failed for %s (%s)", inst.symbol, inst.yahoo_symbol)
                continue
            rows = rows_from_history(history)
            if not rows:
                log.warning("No price rows returned for %s (%s)", inst.symbol, inst.yahoo_symbol)
                continue
            total += upsert_price_rows(session, inst.id, rows, source="yfinance")
            if start is None:
                log.info(
                    "Backfilled full history for %s: %d rows (%s .. %s)",
                    inst.symbol,
                    len(rows),
                    rows[0]["date"],
                    rows[-1]["date"],
                )
            else:
                log.info("Upserted %d price rows for %s (since %s)", len(rows), inst.symbol, start)
    return total


def ingest_equity_prices() -> int:
    return ingest_yahoo_prices("equity")


def ingest_index_prices() -> int:
    return ingest_yahoo_prices("index")
