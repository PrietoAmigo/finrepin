"""Interest rates from FRED's free, key-less CSV download endpoint.

FRED publishes every series as a downloadable CSV at
``https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>`` — no API
key required (it's the same endpoint pandas-datareader uses). Each observation
is stored as a daily ``close`` on the instrument, mirroring how forex/crypto
spot rows carry only ``close``. FRED writes missing observations as ``.``,
which we skip, so a slow-moving monthly series simply has monthly points.

Fetches are state-aware like the Yahoo price path: the first run with no
FRED-sourced rows backfills the full history; later runs re-fetch from a few
days before the latest stored observation, so gaps and revisions self-heal.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.fred
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from typing import Any

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.db import session_scope
from fintracker.ingest.prices import incremental_start, upsert_price_rows
from fintracker.models import Instrument, Price

log = logging.getLogger(__name__)

FREDGRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# FRED's CSV endpoint needs no key. Deliberately DON'T set a custom User-Agent:
# FRED's CDN tarpits custom/browser-like agents (the connection opens, the TLS
# handshake completes, then the response never arrives and the read times out),
# while it serves requests' default `python-requests/<ver>` agent instantly.
# The timeout is a (connect, read) tuple to give reads a little more headroom.
_TIMEOUT = (10, 60)


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def fetch_fred_csv(series_id: str, start: dt.date | None = None) -> str:
    """Download a FRED series as CSV; `start` maps to FRED's `cosd` (from) param."""
    params = {"id": series_id}
    if start is not None:
        params["cosd"] = start.isoformat()
    resp = requests.get(FREDGRAPH_CSV, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def rows_from_csv(text: str) -> list[dict[str, Any]]:
    """Parse a fredgraph CSV into upsertable price dicts, oldest first.

    The CSV is a DATE column plus one value column named after the series id.
    Missing observations are written as ``.`` (and occasionally blank); both are
    skipped so only real observations are stored.
    """
    rows: dict[dt.date, dict[str, Any]] = {}
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or len(header) < 2:
        return []
    for record in reader:
        if len(record) < 2:
            continue
        raw_date, raw_value = record[0].strip(), record[1].strip()
        if not raw_date or raw_value in ("", "."):
            continue
        try:
            date = dt.date.fromisoformat(raw_date)
            value = float(raw_value)
        except ValueError:
            continue
        rows[date] = {
            "date": date,
            "open": None,
            "high": None,
            "low": None,
            "close": value,
            "volume": None,
        }
    return [rows[k] for k in sorted(rows)]


def _stored_fred_bounds(
    session: Session, instrument_id: int
) -> tuple[dt.date | None, dt.date | None]:
    """(earliest, latest) date of FRED-sourced rows for the instrument."""
    earliest, latest = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.instrument_id == instrument_id, Price.source == "fred"
        )
    ).one()
    return earliest, latest


def ingest_interest_rates() -> int:
    """Fetch + upsert the FRED series for every `kind='rate'` instrument."""
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "rate", Instrument.fred_series.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.fred_series is not None
            earliest, latest = _stored_fred_bounds(session, inst.id)
            start = incremental_start(earliest, latest, dt.date.today())
            try:
                text = fetch_fred_csv(inst.fred_series, start=start)
            except Exception:
                log.exception("FRED fetch failed for %s (%s)", inst.symbol, inst.fred_series)
                continue
            rows = rows_from_csv(text)
            if not rows:
                log.warning("No FRED rows returned for %s (%s)", inst.symbol, inst.fred_series)
                continue
            total += upsert_price_rows(session, inst.id, rows, source="fred")
            if start is None:
                log.info(
                    "Backfilled full FRED history for %s: %d rows (%s .. %s)",
                    inst.symbol,
                    len(rows),
                    rows[0]["date"],
                    rows[-1]["date"],
                )
            else:
                log.info("Upserted %d FRED rows for %s (since %s)", len(rows), inst.symbol, start)
    return total


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_interest_rates()
