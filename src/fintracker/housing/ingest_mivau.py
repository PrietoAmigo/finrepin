"""House prices (€/m²) from the Ministerio de Vivienda (MIVAU / ex-Fomento).

The ministry publishes its house-price statistics as **legacy ``.XLS``
spreadsheets** (the "BoletinOnline" sedal files), not a JSON API. Each workbook is
a wide table — regions down the rows (national, communities, provinces), quarters
across the columns — under a few title rows. This ingest downloads each workbook,
finds the header row automatically, and maps every region row into observations
at each level it matches (a "Madrid" row feeds both province and community).

Defaults point at the current sedal files; override any with its ``MIVAU_*_URL``
env var. The whole statistic is the appraised (tasado) value, so the four series
are: all free-market, new (≤5y), second-hand (>5y), and protected (VPO).

⚠️ Best-effort: written to the documented shape but not validated against the
live files (the build environment could not reach the ministry). If a workbook's
layout differs, adjust ``sheet`` or the header detection.

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
from fintracker.housing.regions import region_codes_for_name
from fintracker.housing.seed import clear_sample_observations
from fintracker.models import RegionObservation

log = logging.getLogger(__name__)

SOURCE = "MIVAU"
_TIMEOUT = (10, 120)
_UPSERT_CHUNK = 500
_SEDAL = "https://apps.fomento.gob.es/BoletinOnline2/sedal"

# Period headers like "2024T1", "1T2024", "2024TI", "2024-Q1", "2024".
_QUARTER_RE = re.compile(r"(?:(\d{4})\s*[t q]\s*([1-4ivx]+))|(?:([1-4])\s*t\s*(\d{4}))", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


@dataclass(frozen=True)
class MivauSpec:
    indicator: str
    url_env: str  # env var holding the workbook URL (overrides default_url)
    default_url: str
    sheet: str | int = 0


MIVAU_SPECS: list[MivauSpec] = [
    MivauSpec("price_eur_m2", "MIVAU_PRICE_URL", f"{_SEDAL}/35101000.XLS"),
    MivauSpec("price_eur_m2_new", "MIVAU_PRICE_NEW_URL", f"{_SEDAL}/35101500.XLS"),
    MivauSpec("price_eur_m2_used", "MIVAU_PRICE_USED_URL", f"{_SEDAL}/35102000.XLS"),
    # 35102500 is protected housing (VPO) — a distinct series, not a re-appraisal
    # of the free-market price.
    MivauSpec("price_eur_m2_protected", "MIVAU_PROTECTED_URL", f"{_SEDAL}/35102500.XLS"),
]


def parse_period(label: str) -> dt.date | None:
    """Parse a MIVAU column header into a period start date. Pure."""
    text = str(label).strip().lower()
    plain_year = re.fullmatch(r"(\d{4})(?:\.0)?", text)
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


def detect_header_row(raw: pd.DataFrame) -> int | None:
    """Row index (0-based) with the most period-parseable cells. Pure.

    MIVAU sheets carry title rows above the region/quarter grid, so the header is
    not row 0 — it's the row where the quarter/year columns live.
    """
    best_row, best_hits = None, 0
    for i in range(min(len(raw), 25)):
        hits = sum(1 for v in raw.iloc[i].tolist() if parse_period(str(v)) is not None)
        if hits > best_hits:
            best_row, best_hits = i, hits
    return best_row if best_hits >= 2 else None


def frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame | None:
    """Turn a header-less sheet into a header'd frame using the detected row."""
    header_row = detect_header_row(raw)
    if header_row is None:
        return None
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = [str(v) for v in raw.iloc[header_row].tolist()]
    return body


def rows_from_frame(frame: pd.DataFrame) -> list[tuple[str, dt.date, float]]:
    """Parse a wide MIVAU frame into (region_code, period, value) rows. Pure.

    The first non-period column is the region column; each region name is mapped
    to every level it matches, so one sheet fills nation/community/province.
    """
    columns = list(frame.columns)
    period_cols = [(c, parse_period(str(c))) for c in columns]
    region_col = next((c for c, p in period_cols if p is None), None)
    if region_col is None:
        return []
    rows: list[tuple[str, dt.date, float]] = []
    for _, record in frame.iterrows():
        codes = region_codes_for_name(str(record[region_col]))
        if not codes:
            continue
        for col, period in period_cols:
            if period is None or col == region_col:
                continue
            value = record[col]
            if pd.isna(value):
                continue
            try:
                numeric = float(value)
            except (ValueError, TypeError):
                continue
            for code in codes:
                rows.append((code, period, numeric))
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


def _read_raw(content: bytes, url: str) -> pd.DataFrame | None:
    """Read a workbook into a header-less frame; ``.xls`` via xlrd, else openpyxl.

    Falls back to parsing an HTML table, since some ministry ``.XLS`` downloads
    are actually HTML.
    """
    engine = "xlrd" if url.lower().endswith(".xls") else "openpyxl"
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=0, header=None, engine=engine)
    except Exception:
        try:
            tables = pd.read_html(io.BytesIO(content), header=None)
        except Exception:
            return None
        return max(tables, key=lambda t: t.shape[0]) if tables else None


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
    url = os.environ.get(spec.url_env, "").strip() or spec.default_url
    try:
        raw = _read_raw(_download(url), url)
    except Exception:
        log.exception("MIVAU download failed for %s (%s)", spec.indicator, url)
        return 0
    frame = frame_from_raw(raw) if raw is not None else None
    if frame is None:
        log.warning("MIVAU: could not locate a header row for %s (%s)", spec.indicator, url)
        return 0
    parsed = rows_from_frame(frame)
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
