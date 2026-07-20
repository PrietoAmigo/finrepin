"""Spanish house prices from INE's free, key-less Tempus3 JSON API.

INE (Instituto Nacional de Estadística) publishes every statistical series as
JSON at ``https://servicios.ine.es/wstempus/js/ES/<function>/<input>`` — no API
key. This module ingests the **House Price Index** (Índice de Precios de
Vivienda, IPV): a quarterly index, base 2015=100, by autonomous community with a
national total, split into overall / new-build / resale components.

Two endpoints are used:

* ``TABLAS_OPERACION/IPV`` lists the tables of the IPV operation. We pick the
  by-community table at runtime (matching its title) so the ingest self-heals if
  INE renumbers a table; ``INE_IPV_TABLE`` overrides the choice.
* ``DATOS_TABLA/<id>`` returns every series in that table with its full history.

Each series is classified into (region, indicator) from its labels — index
series only; variation/rate series are skipped and year-on-year change is derived
downstream. Fetches are state-aware like the market ingestors: the first run
backfills the full history, later runs re-fetch the last few quarters so
revisions self-heal.

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.ingest
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.regions import (
    INDICATORS_BY_CODE,
    normalize,
    region_code_from_ine_name,
)
from fintracker.models import HousingPrice

log = logging.getLogger(__name__)

SOURCE = "ine"
IPV_OPERATION = "IPV"

# (connect, read) timeout. INE's default agent is fine; no custom User-Agent.
_TIMEOUT = (10, 60)
# Quarters re-fetched on an incremental run (2 years of overlap for revisions).
_INCREMENTAL_QUARTERS = 8
_UPSERT_CHUNK_SIZE = 500

# Labels that mark a non-index series (a variation/rate or an annual average),
# which we skip — YoY change is derived from the stored index downstream.
_SKIP_TOKENS = ("variacion", "tasa", "media anual")


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def _fetch_json(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a Tempus3 endpoint (path relative to ``INE_BASE_URL``) as JSON."""
    base = get_settings().ine_base_url.rstrip("/")
    resp = requests.get(f"{base}/{path.lstrip('/')}", params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def list_ipv_tables() -> list[dict[str, Any]]:
    """Tables of the IPV operation: ``[{"Id": ..., "Nombre": ...}, ...]``."""
    data = _fetch_json(f"TABLAS_OPERACION/{IPV_OPERATION}")
    return data if isinstance(data, list) else []


def choose_ccaa_table(tables: list[dict[str, Any]]) -> str | None:
    """Pick the by-community IPV table id from a ``TABLAS_OPERACION`` listing.

    Prefer a title naming both the communities and the components (overall /
    new / resale); fall back to any community-level, non-grouped table. Pure so
    the selection heuristic is unit-testable.
    """
    best: str | None = None
    fallback: str | None = None
    for table in tables:
        name = normalize(str(table.get("Nombre", "")))
        table_id = table.get("Id")
        if table_id is None:
            continue
        table_id = str(table_id)
        if "comunidad" not in name and "ccaa" not in name:
            continue
        if "grupo" in name:  # "por grupos" tables are a different breakdown
            continue
        if fallback is None:
            fallback = table_id
        if "general" in name or "componente" in name or "nueva" in name:
            best = best or table_id
    return best or fallback


def _labels(series: dict[str, Any]) -> list[str]:
    """Every classification-relevant label of a series, most specific first.

    Metadata dimension values come first (each is a single dimension value like
    "Andalucía" or "Vivienda nueva"), then the dotted segments of the series
    name as a fallback when metadata is thin.
    """
    labels: list[str] = []
    for meta in series.get("MetaData") or []:
        name = meta.get("Nombre")
        if isinstance(name, str) and name.strip():
            labels.append(name.strip())
    name = series.get("Nombre")
    if isinstance(name, str):
        labels.extend(seg.strip() for seg in name.split(".") if seg.strip())
    return labels


def _component_indicator(labels: list[str]) -> str | None:
    """Map a series' labels to an IPV component indicator code, or None."""
    for label in labels:
        norm = normalize(label)
        if "segunda" in norm:  # "vivienda de segunda mano"
            return "ipv_secondhand"
        if "nueva" in norm:  # "vivienda nueva"
            return "ipv_new"
    for label in labels:
        norm = normalize(label)
        # "General" is its own dimension value / name segment — match it exactly
        # so a descriptive title ("IPV general por CCAA") can't tag other series.
        if norm == "general" or norm.startswith("general ") or norm.endswith(" general"):
            return "ipv_general"
    return None


def classify_series(series: dict[str, Any]) -> tuple[str, str] | None:
    """Classify a DATOS_TABLA series into ``(region_code, indicator)`` or None.

    Returns None for series we don't store: variation/rate series, or ones whose
    region or component can't be identified. Pure — the heart of the parser and
    the main unit-tested unit.
    """
    labels = _labels(series)
    joined = normalize(" ".join(labels))
    if any(tok in joined for tok in _SKIP_TOKENS):
        return None
    region = next(filter(None, (region_code_from_ine_name(lbl) for lbl in labels)), None)
    if region is None:
        return None
    indicator = _component_indicator(labels)
    if indicator is None:
        return None
    return region, indicator


def _quarter_start(day: dt.date) -> dt.date:
    """First day of the calendar quarter containing ``day`` (period alignment)."""
    return dt.date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)


def rows_from_data(data: list[dict[str, Any]]) -> list[tuple[dt.date, float]]:
    """Parse a series' ``Data`` array into (quarter_start, value), oldest first.

    ``Fecha`` is epoch milliseconds; missing/null ``Valor`` points are skipped.
    Points are de-duplicated per quarter (last one wins).
    """
    out: dict[dt.date, float] = {}
    for point in data:
        fecha, valor = point.get("Fecha"), point.get("Valor")
        if fecha is None or valor is None:
            continue
        try:
            day = dt.datetime.fromtimestamp(int(fecha) / 1000, tz=dt.UTC).date()
            value = float(valor)
        except (ValueError, TypeError, OverflowError):
            continue
        out[_quarter_start(day)] = value
    return [(period, out[period]) for period in sorted(out)]


def parse_table(series_list: list[dict[str, Any]]) -> list[tuple[str, str, dt.date, float]]:
    """Flatten a DATOS_TABLA response into (region, indicator, period, value) rows.

    Pure: the whole network-free parse, from raw series to upsertable tuples.
    """
    rows: list[tuple[str, str, dt.date, float]] = []
    for series in series_list:
        classified = classify_series(series)
        if classified is None:
            continue
        region, indicator = classified
        if indicator not in INDICATORS_BY_CODE:
            continue
        for period, value in rows_from_data(series.get("Data") or []):
            rows.append((region, indicator, period, value))
    return rows


def _resolve_table_id() -> str | None:
    """The configured IPV table id, else the one discovered from INE."""
    configured = get_settings().ine_ipv_table.strip()
    if configured:
        return configured
    try:
        table_id = choose_ccaa_table(list_ipv_tables())
    except Exception:
        log.exception("INE table discovery failed")
        return None
    if table_id is None:
        log.warning("No by-community IPV table found in TABLAS_OPERACION/%s", IPV_OPERATION)
    return table_id


def _has_ine_rows() -> bool:
    with session_scope() as session:
        return (
            session.execute(
                select(func.count()).select_from(HousingPrice).where(HousingPrice.source == SOURCE)
            ).scalar_one()
            > 0
        )


def _upsert(rows: list[tuple[str, str, dt.date, float]]) -> int:
    if not rows:
        return 0
    with session_scope() as session:
        for offset in range(0, len(rows), _UPSERT_CHUNK_SIZE):
            chunk = rows[offset : offset + _UPSERT_CHUNK_SIZE]
            stmt = pg_insert(HousingPrice).values(
                [
                    {
                        "region_code": region,
                        "indicator": indicator,
                        "period": period,
                        "value": value,
                        "source": SOURCE,
                    }
                    for region, indicator, period, value in chunk
                ]
            )
            session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_housing_region_indicator_period",
                    set_={"value": stmt.excluded.value, "source": stmt.excluded.source},
                )
            )
    return len(rows)


def ingest_housing() -> int:
    """Fetch + upsert INE IPV house-price index rows. Returns rows written."""
    table_id = _resolve_table_id()
    if table_id is None:
        return 0

    params: dict[str, Any] = {"det": 2}
    if _has_ine_rows():  # incremental: only the last few quarters
        params["nult"] = _INCREMENTAL_QUARTERS

    try:
        series_list = _fetch_json(f"DATOS_TABLA/{table_id}", params=params)
    except Exception:
        log.exception("INE DATOS_TABLA fetch failed for table %s", table_id)
        return 0
    if not isinstance(series_list, list):
        log.warning("INE DATOS_TABLA/%s returned no series list", table_id)
        return 0

    rows = parse_table(series_list)
    if not rows:
        log.warning(
            "Parsed 0 housing rows from INE table %s (%d series)", table_id, len(series_list)
        )
        return 0

    written = _upsert(rows)
    regions = {r[0] for r in rows}
    periods = sorted({r[2] for r in rows})
    log.info(
        "Ingested %d INE IPV rows from table %s: %d regions, %s .. %s",
        written,
        table_id,
        len(regions),
        periods[0],
        periods[-1],
    )
    return written


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_housing()
