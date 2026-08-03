"""Precious metals (gold, silver) from Stooq's free, key-less CSV download.

Stooq publishes every symbol as a daily CSV at
``https://stooq.com/q/d/l/?s=<symbol>&i=d`` — no API key, no registration — and
it carries decades of daily history for the spot metals (`xauusd`, `xagusd`),
quoted in **USD per troy ounce**. That makes it the closest free equivalent of
the LBMA spot price, so it is the primary source here.

Stooq throttles heavy use (it answers a plain-text "Exceeded the daily hits
limit" instead of a CSV) and occasionally serves an empty body. Neither is an
HTTP error, so the parser returns no rows for them and the ingest falls back to
Yahoo's continuous front-month futures (``GC=F``/``SI=F``), which track spot
within roughly a percent. Every row records which source it came from, so a
fallback day is visible in `prices.source`.

Fetches are state-aware like the rest of the price path: the first run with no
stored rows backfills the full history; later runs re-fetch from a few days
before the latest stored bar, so gaps and revisions self-heal.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.metals
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections.abc import Callable
from typing import Any

import pandas as pd
import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from fintracker.db import session_scope
from fintracker.ingest.prices import (
    fetch_daily_history,
    incremental_start,
    rows_from_history,
    upsert_price_rows,
)
from fintracker.models import Instrument, Price

log = logging.getLogger(__name__)

# The .com host and its .pl mirror serve the same CSV; some networks get a 404
# from one and a normal file from the other, so both are tried in order.
STOOQ_BASE_URLS = ("https://stooq.com/q/d/l/", "https://stooq.pl/q/d/l/")

# Stooq answers the default `python-requests/<ver>` agent with a 404 even for
# symbols it serves fine in a browser, so send a browser-like User-Agent (the
# same accommodation pandas-datareader's Stooq reader makes).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}

# Stooq is a plain static-file host; a (connect, read) tuple keeps a slow
# full-history download from hanging the whole ingest.
_TIMEOUT = (10, 60)

# Column names Stooq uses, lowercased. Volume is absent for some symbols.
_OHLC_COLUMNS = ("open", "high", "low", "close")


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport failures and 5xx, never a 4xx.

    A 404/403 from Stooq is a settled answer — retrying it four times with
    exponential backoff just stalls the whole market ingest before the Yahoo
    fallback gets its turn.
    """
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, requests.RequestException)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, max=15),
    reraise=True,
)
def _get_csv(base_url: str, params: dict[str, str]) -> str:
    resp = requests.get(base_url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def fetch_stooq_csv(
    symbol: str, start: dt.date | None = None, today: dt.date | None = None
) -> str:
    """Download a Stooq symbol's daily history as CSV.

    `start` maps to Stooq's `d1` (from) parameter; it only honours a range when
    both ends are given, so `d2` is pinned to today. Without `start` the whole
    available history comes back. Each host in `STOOQ_BASE_URLS` is tried in
    turn; the last error is raised if none answers.
    """
    params = {"s": symbol, "i": "d"}
    if start is not None:
        params["d1"] = start.strftime("%Y%m%d")
        params["d2"] = (today or dt.date.today()).strftime("%Y%m%d")

    last_error: Exception | None = None
    for base_url in STOOQ_BASE_URLS:
        try:
            return _get_csv(base_url, params)
        except requests.RequestException as exc:
            log.warning("Stooq %s did not serve %s: %s", base_url, symbol, exc)
            last_error = exc
    raise last_error if last_error is not None else RuntimeError("no Stooq host configured")


def rows_from_stooq_csv(text: str) -> list[dict[str, Any]]:
    """Parse a Stooq daily CSV into upsertable price dicts, oldest first.

    The CSV is ``Date,Open,High,Low,Close`` (plus ``Volume`` for symbols that
    have one). Columns are read by header name because that order is not
    guaranteed. Anything that isn't a CSV with a Date header — Stooq's
    rate-limit message, an empty body, an error page — yields no rows, which is
    the ingest's signal to fall back to the secondary source.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return []
    columns = {name.strip().lower(): idx for idx, name in enumerate(header)}
    if "date" not in columns or "close" not in columns:
        return []

    def _cell(record: list[str], name: str) -> float | None:
        idx = columns.get(name)
        if idx is None or idx >= len(record):
            return None
        try:
            return float(record[idx].strip())
        except ValueError:  # 'N/A', '-', blank
            return None

    rows: dict[dt.date, dict[str, Any]] = {}
    for record in reader:
        if not record:
            continue
        try:
            date = dt.date.fromisoformat(record[columns["date"]].strip())
        except (ValueError, IndexError):
            continue
        close = _cell(record, "close")
        if close is None:
            continue
        volume = _cell(record, "volume")
        rows[date] = {
            "date": date,
            **{col: _cell(record, col) for col in _OHLC_COLUMNS if col != "close"},
            "close": close,
            "volume": None if volume is None else int(volume),
        }
    return [rows[k] for k in sorted(rows)]


def metal_rows(
    inst: Instrument,
    start: dt.date | None,
    *,
    fetch_stooq: Callable[..., str] | None = None,
    fetch_yahoo: Callable[..., pd.DataFrame] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Daily rows for one metal as (rows, source), preferring Stooq spot.

    Falls back to the instrument's Yahoo futures symbol when Stooq is not
    configured, errors, or returns nothing usable (its rate-limit message).
    Returns an empty list and an empty source when no source produced rows;
    the fetchers are injectable so the preference order is unit-testable.
    """
    fetch_stooq = fetch_stooq or fetch_stooq_csv
    fetch_yahoo = fetch_yahoo or fetch_daily_history

    if inst.stooq_symbol:
        try:
            rows = rows_from_stooq_csv(fetch_stooq(inst.stooq_symbol, start=start))
        except requests.RequestException as exc:
            # An expected, recoverable outcome — the fallback below covers it,
            # so log one line rather than a traceback.
            log.warning("Stooq fetch failed for %s (%s): %s", inst.symbol, inst.stooq_symbol, exc)
            rows = []
        except Exception:
            log.exception("Stooq fetch failed for %s (%s)", inst.symbol, inst.stooq_symbol)
            rows = []
        if rows:
            return rows, "stooq"
        log.warning(
            "No Stooq rows for %s (%s) — falling back to Yahoo futures.",
            inst.symbol,
            inst.stooq_symbol,
        )

    if inst.yahoo_symbol:
        try:
            rows = rows_from_history(fetch_yahoo(inst.yahoo_symbol, start=start))
        except Exception:
            log.exception("Yahoo fetch failed for %s (%s)", inst.symbol, inst.yahoo_symbol)
            return [], ""
        if rows:
            return rows, "yfinance"

    return [], ""


def _stored_bounds(session: Session, instrument_id: int) -> tuple[dt.date | None, dt.date | None]:
    """(earliest, latest) stored bar for the instrument, across both sources.

    Unlike the Yahoo path this doesn't filter by source: a metal's rows may be
    a mix of Stooq spot and Yahoo futures (whichever answered on a given run),
    and the incremental window should follow the series as a whole.
    """
    earliest, latest = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.instrument_id == instrument_id
        )
    ).one()
    return earliest, latest


def _stored_source(session: Session, instrument_id: int) -> str | None:
    """Source of the newest stored bar, or None when there is none yet."""
    return session.execute(
        select(Price.source)
        .where(Price.instrument_id == instrument_id)
        .order_by(Price.date.desc())
        .limit(1)
    ).scalar_one_or_none()


def ingest_metal_prices() -> int:
    """Fetch + upsert daily bars for every `kind='metal'` instrument."""
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(select(Instrument).where(Instrument.kind == "metal")).scalars().all()
        )
        for inst in instruments:
            earliest, latest = _stored_bounds(session, inst.id)
            stored_source = _stored_source(session, inst.id)
            start = incremental_start(earliest, latest, dt.date.today())
            rows, source = metal_rows(inst, start)
            if not rows:
                log.warning("No price rows returned for %s from any source", inst.symbol)
                continue
            if start is not None and stored_source is not None and source != stored_source:
                # The answering source changed (Stooq spot came back, or it went
                # away and Yahoo futures took over). Re-fetch the whole history
                # from the new source so the stored series stays one series
                # rather than a splice of spot and futures at the switch date.
                log.info(
                    "Source for %s changed %s -> %s; re-backfilling full history.",
                    inst.symbol,
                    stored_source,
                    source,
                )
                full_rows, full_source = metal_rows(inst, None)
                if full_rows:
                    rows, source, start = full_rows, full_source, None
            total += upsert_price_rows(session, inst.id, rows, source=source)
            if start is None:
                log.info(
                    "Backfilled full %s history for %s: %d rows (%s .. %s)",
                    source,
                    inst.symbol,
                    len(rows),
                    rows[0]["date"],
                    rows[-1]["date"],
                )
            else:
                log.info(
                    "Upserted %d %s rows for %s (since %s)", len(rows), source, inst.symbol, start
                )
    return total


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_metal_prices()
