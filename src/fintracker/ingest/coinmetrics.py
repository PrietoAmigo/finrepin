"""Bitcoin on-chain valuation series from the Coin Metrics Community API.

Coin Metrics publishes a free, key-less community tier of its Network Data API
at ``https://community-api.coinmetrics.io/v4/timeseries/asset-metrics`` — no API
key required. We use it for the two inputs to Bitcoin's MVRV Z-Score:

* ``CapMrktCurUSD`` — market cap (current supply x price), and
* ``CapRealUSD``   — realized cap (each coin valued at the price it last moved).

Each daily observation is stored as a ``close`` on its instrument, exactly like
the FRED/ECB rate paths, so the dashboard treats it identically. The MVRV
Z-Score itself is derived in the panel SQL — ``(market cap - realized cap) /
stddev(market cap)`` over the full stored history — so it self-calibrates as
history grows (the same approach the BTC rainbow chart takes with its
regression).

Fetches are state-aware like the Yahoo/FRED/ECB paths: the first run with no
Coin Metrics-sourced rows backfills the full history; later runs re-fetch from a
few days before the latest stored observation, so gaps and revisions self-heal.

Run one off-schedule ingest by hand with:
    python -m fintracker.ingest.coinmetrics
"""

from __future__ import annotations

import datetime as dt
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

COINMETRICS_ASSET_METRICS = (
    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
)

# Every on-chain metric tracked here is Bitcoin's; keep the asset fixed until a
# second asset actually needs one (then it becomes an instrument column).
_ASSET = "btc"
# The community tier caps page_size at 10000; BTC's daily history is ~5.8k rows,
# so a backfill is a single page. The page cap is a belt-and-braces stop.
_PAGE_SIZE = 10000
_MAX_PAGES = 50
# (connect, read) timeout. No custom User-Agent — requests' default is fine here.
_TIMEOUT = (10, 60)


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_asset_metric(
    metric: str, start: dt.date | None = None, asset: str = _ASSET
) -> list[dict[str, Any]]:
    """Fetch a daily Coin Metrics metric series, following pagination.

    Returns the raw Coin Metrics data points (dicts) across all pages, oldest
    first as the API returns them. `start` maps to the API's inclusive
    `start_time`; omit it to pull the full available history.
    """
    params: dict[str, Any] = {
        "assets": asset,
        "metrics": metric,
        "frequency": "1d",
        "page_size": _PAGE_SIZE,
    }
    if start is not None:
        params["start_time"] = start.isoformat()

    data: list[dict[str, Any]] = []
    url: str | None = COINMETRICS_ASSET_METRICS
    # The first request carries the query params; each next_page_url already
    # encodes them, so only the first call passes `params`.
    next_params: dict[str, Any] | None = params
    for _ in range(_MAX_PAGES):
        if url is None:
            break
        payload = _get_json(url, next_params)
        data.extend(payload.get("data") or [])
        url = payload.get("next_page_url")
        next_params = None
    return data


def rows_from_metric(data: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Parse Coin Metrics data points for `metric` into upsertable price dicts.

    Each point carries a `time` (ISO 8601 timestamp) and the metric id as a key
    whose value is the number as a string. Points missing the value are skipped;
    the daily value lands in `close`, like a rate/forex/crypto-spot row. Sorted
    oldest first, deduped by date (last write wins).
    """
    rows: dict[dt.date, dict[str, Any]] = {}
    for point in data:
        raw_time = str(point.get("time") or "")
        raw_value = point.get(metric)
        if not raw_time or raw_value is None or raw_value == "":
            continue
        try:
            date = dt.date.fromisoformat(raw_time[:10])
            value = float(raw_value)
        except (ValueError, TypeError):
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


def _stored_coinmetrics_bounds(
    session: Session, instrument_id: int
) -> tuple[dt.date | None, dt.date | None]:
    """(earliest, latest) date of Coin Metrics-sourced rows for the instrument."""
    earliest, latest = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.instrument_id == instrument_id, Price.source == "coinmetrics"
        )
    ).one()
    return earliest, latest


def ingest_onchain_metrics() -> int:
    """Fetch + upsert the Coin Metrics series for every `kind='onchain'` instrument."""
    total = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "onchain",
                    Instrument.coinmetrics_metric.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.coinmetrics_metric is not None
            earliest, latest = _stored_coinmetrics_bounds(session, inst.id)
            start = incremental_start(earliest, latest, dt.date.today())
            try:
                data = fetch_asset_metric(inst.coinmetrics_metric, start=start)
            except Exception:
                log.exception(
                    "Coin Metrics fetch failed for %s (%s)",
                    inst.symbol,
                    inst.coinmetrics_metric,
                )
                continue
            rows = rows_from_metric(data, inst.coinmetrics_metric)
            if not rows:
                log.warning(
                    "No Coin Metrics rows returned for %s (%s)",
                    inst.symbol,
                    inst.coinmetrics_metric,
                )
                continue
            total += upsert_price_rows(session, inst.id, rows, source="coinmetrics")
            if start is None:
                log.info(
                    "Backfilled full Coin Metrics history for %s: %d rows (%s .. %s)",
                    inst.symbol,
                    len(rows),
                    rows[0]["date"],
                    rows[-1]["date"],
                )
            else:
                log.info(
                    "Upserted %d Coin Metrics rows for %s (since %s)",
                    len(rows),
                    inst.symbol,
                    start,
                )
    return total


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_onchain_metrics()
