"""Daily OHLCV bars for Yahoo-covered instruments (equities + forex)."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Instrument, Price

log = logging.getLogger(__name__)

# A few days of lookback so weekends/holidays and a missed run self-heal.
HISTORY_PERIOD = "7d"


def fetch_daily_history(yahoo_symbol: str, period: str = HISTORY_PERIOD) -> pd.DataFrame:
    return yf.Ticker(yahoo_symbol).history(period=period, interval="1d", auto_adjust=False)


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
    for row in rows:
        stmt = pg_insert(Price).values(instrument_id=instrument_id, source=source, **row)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_prices_instrument_date",
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "source": stmt.excluded.source,
                },
            )
        )
    return len(rows)


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
            try:
                history = fetch_daily_history(inst.yahoo_symbol)
            except Exception:
                log.exception("Price fetch failed for %s (%s)", inst.symbol, inst.yahoo_symbol)
                continue
            rows = rows_from_history(history)
            if not rows:
                log.warning("No price rows returned for %s (%s)", inst.symbol, inst.yahoo_symbol)
                continue
            total += upsert_price_rows(session, inst.id, rows, source="yfinance")
            log.info("Upserted %d price rows for %s", len(rows), inst.symbol)
    return total


def ingest_equity_prices() -> int:
    return ingest_yahoo_prices("equity")
