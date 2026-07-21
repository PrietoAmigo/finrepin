"""House prices (€/m²) from the Ministerio de Vivienda (MIVAU / MITMA).

The ministry publishes its house-price statistics as **spreadsheets**, not a
JSON API, and the download URLs and sheet layouts change between releases. So
this ingest is URL-driven: point each indicator at the current workbook via an
env var, and a generic "wide table" parser (regions down the rows, periods
across the columns) maps it into observations.

⚠️ Best-effort and **unconfigured by default**: with no ``MIVAU_*_URL`` set it
does nothing (the dashboard shows sample €/m² until then). It could not be
validated against the live files (the build environment can't reach
``mivau.gob.es``). Set the URLs (see ``.env.example``); adjust ``header_row`` /
``level`` if a sheet's layout differs.

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.ingest_mivau
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
import re
from dataclasses import dataclass

import pandas as pd
import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.regions import region_code_from_ine_name
from fintracker.housing.seed import clear_sample_observations
from fintracker.models import RegionObservation

log = logging.getLogger(__name__)

SOURCE = "MIVAU"
_TIMEOUT = (10, 120)
_UPSERT_CHUNK = 500

# Period headers like "2024T1", "1T2024", "2024TI", "2024-Q1", "2024".
_QUARTER_RE = re.compile(r"(?:(\d{4})\s*[t q]\s*([1-4ivx]+))|(?:([1-4])\s*t\s*(\d{4}))", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


@dataclass(frozen=True)
class MivauSpec:
    indicator: str
    url_env: str  # env var holding the workbook URL
    level: str  # ccaa | prov | muni
    sheet: str | int = 0
    header_row: int = 0


MIVAU_SPECS: list[MivauSpec] = [
    MivauSpec("price_eur_m2", "MIVAU_PRICE_URL", "prov"),
    MivauSpec("price_eur_m2_new", "MIVAU_PRICE_NEW_URL", "prov"),
    MivauSpec("price_eur_m2_used", "MIVAU_PRICE_USED_URL", "prov"),
    MivauSpec("appraisal_eur_m2", "MIVAU_APPRAISAL_URL", "prov"),
]


def parse_period(label: str) -> dt.date | None:
    """Parse a MIVAU column header into a period start date. Pure."""
    text = str(label).strip().lower()
    plain_year = re.fullmatch(r"(\d{4})", text)
    if plain_year:
        return dt.date(int(plain_year.group(1)), 1, 1)
    match = _QUARTER_RE.search(text)
    if not match:
        return None
    if match.group(1):
        year, q_raw = int(match.group(1)), match.group(2)
    else:
        year, q_raw = int(match.group(4)), match.group(3)
    quarter = _ROMAN.get(q_raw) if q_raw in _ROMAN else (int(q_raw) if q_raw.isdigit() else None)
    if quarter is None or not 1 <= quarter <= 4:
        return None
    return dt.date(year, (quarter - 1) * 3 + 1, 1)


def rows_from_frame(frame: pd.DataFrame, level: str) -> list[tuple[str, dt.date, float]]:
    """Parse a wide MIVAU frame (region column + period columns). Pure-ish.

    The first column whose header does not parse as a period is taken as the
    region-name column; every parseable period column becomes an observation.
    """
    columns = list(frame.columns)
    period_cols = [(c, parse_period(str(c))) for c in columns]
    region_col = next((c for c, p in period_cols if p is None), None)
    if region_col is None:
        return []
    rows: list[tuple[str, dt.date, float]] = []
    for _, record in frame.iterrows():
        region = region_code_from_ine_name(str(record[region_col]), level)
        if region is None:
            continue
        for col, period in period_cols:
            if period is None or col == region_col:
                continue
            value = record[col]
            if pd.isna(value):
                continue
            try:
                rows.append((region, period, float(value)))
            except (ValueError, TypeError):
                continue
    return rows


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def _download(url: str) -> bytes:
    resp = requests.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _upsert(rows: list[tuple[str, str, dt.date, float]]) -> int:
    with session_scope() as session:
        for offset in range(0, len(rows), _UPSERT_CHUNK):
            chunk = rows[offset : offset + _UPSERT_CHUNK]
            stmt = pg_insert(RegionObservation).values(
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
                    constraint="uq_region_obs_region_indicator_period",
                    set_={"value": stmt.excluded.value, "source": stmt.excluded.source},
                )
            )
    return len(rows)


def ingest_spec(spec: MivauSpec) -> int:
    url = os.environ.get(spec.url_env, "").strip()
    if not url:
        log.info("MIVAU %s skipped: %s not set.", spec.indicator, spec.url_env)
        return 0
    try:
        content = _download(url)
        frame = pd.read_excel(
            io.BytesIO(content), sheet_name=spec.sheet, header=spec.header_row, engine="openpyxl"
        )
    except Exception:
        log.exception("MIVAU fetch/parse failed for %s (%s)", spec.indicator, url)
        return 0
    parsed = rows_from_frame(frame, spec.level)
    if not parsed:
        log.warning("Parsed 0 MIVAU rows for %s from %s", spec.indicator, url)
        return 0
    rows = [(region, spec.indicator, period, value) for region, period, value in parsed]
    written = _upsert(rows)
    clear_sample_observations([spec.indicator])
    log.info(
        "Ingested %d MIVAU rows for %s (%d regions)",
        written, spec.indicator, len({r[0] for r in rows}),
    )
    return written


def ingest_mivau() -> int:
    return sum(ingest_spec(spec) for spec in MIVAU_SPECS)


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_mivau()
