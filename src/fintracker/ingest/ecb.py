"""Interest rates from the ECB Data Portal's free, key-less SDMX REST API.

The ECB Data Portal serves any series as CSV at
``https://data-api.ecb.europa.eu/service/data/<FLOW>/<KEY>?format=csvdata`` —
no API key. We use it for the euro-area benchmark rate, a daily yield-curve
spot rate that is far fresher than FRED's monthly OECD euro-area series (which
lags by months). Each observation is stored as a daily ``close``, exactly like
the FRED path, so the dashboard treats it identically.

Fetches are state-aware like the Yahoo/FRED paths: the first run with no
ECB-sourced rows backfills the full history; later runs re-fetch from a few
days before the latest stored observation so gaps and revisions self-heal.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.ecb
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

ECB_DATA_API = "https://data-api.ecb.europa.eu/service/data"

# (connect, read) timeout. No custom User-Agent — requests' default is fine here.
_TIMEOUT = (10, 60)


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def fetch_ecb_csv(series_key: str, start: dt.date | None = None) -> str:
    """Download an ECB series as CSV.

    `series_key` is the full dotted key (e.g. ``YC.B.U2.EUR.4F.G_N_C.SV_C_YM.SR_10Y``);
    its first segment is the dataflow and the rest is the series key in the URL.
    `start` maps to the ECB's `startPeriod` query parameter.
    """
    flow, _, key = series_key.partition(".")
    params: dict[str, str] = {"format": "csvdata"}
    if start is not None:
        params["startPeriod"] = start.isoformat()
    resp = requests.get(f"{ECB_DATA_API}/{flow}/{key}", params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def rows_from_ecb_csv(text: str) -> list[dict[str, Any]]:
    """Parse an ECB `csvdata` response into upsertable price dicts, oldest first.

    The CSV carries the SDMX dimension columns plus `TIME_PERIOD` and
    `OBS_VALUE`; we locate those two by header name (their position varies by
    dataflow) and skip rows with an empty date or value.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return []
    try:
        date_i = header.index("TIME_PERIOD")
        value_i = header.index("OBS_VALUE")
    except ValueError:
        return []
    rows: dict[dt.date, dict[str, Any]] = {}
    for record in reader:
        if len(record) <= max(date_i, value_i):
            continue
        raw_date, raw_value = record[date_i].strip(), record[value_i].strip()
        if not raw_date or not raw_value:
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


def _stored_ecb_bounds(
    session: Session, instrument_id: int
) -> tuple[dt.date | None, dt.date | None]:
    """(earliest, latest) date of ECB-sourced rows for the instrument."""
    earliest, latest = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.instrument_id == instrument_id, Price.source == "ecb"
        )
    ).one()
    return earliest, latest


def ingest_ecb_rates() -> int:
    """Fetch + upsert the ECB series for every `kind='rate'` instrument with one."""
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "rate", Instrument.ecb_series.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.ecb_series is not None
            earliest, latest = _stored_ecb_bounds(session, inst.id)
            start = incremental_start(earliest, latest, dt.date.today())
            try:
                text = fetch_ecb_csv(inst.ecb_series, start=start)
            except Exception:
                log.exception("ECB fetch failed for %s (%s)", inst.symbol, inst.ecb_series)
                continue
            rows = rows_from_ecb_csv(text)
            if not rows:
                log.warning("No ECB rows returned for %s (%s)", inst.symbol, inst.ecb_series)
                continue
            total += upsert_price_rows(session, inst.id, rows, source="ecb")
            if start is None:
                log.info(
                    "Backfilled full ECB history for %s: %d rows (%s .. %s)",
                    inst.symbol,
                    len(rows),
                    rows[0]["date"],
                    rows[-1]["date"],
                )
            else:
                log.info("Upserted %d ECB rows for %s (since %s)", len(rows), inst.symbol, start)
    return total


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_ecb_rates()
