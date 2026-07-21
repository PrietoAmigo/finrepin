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
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.housing.regions import region_codes_for_name
from fintracker.housing.store import upsert_observations

log = logging.getLogger(__name__)

SOURCE = "MIVAU"
_TIMEOUT = (10, 120)
_SEDAL = "https://apps.fomento.gob.es/BoletinOnline2/sedal"

# Period headers like "2024T1", "2024 T1", "2024-Q1", "2024TI", "1T2024", "2024".
# Roman numerals longest-first so "TIV" isn't read as "TI".
_QUARTER_RE = re.compile(
    r"(?:(\d{4})\s*[-–]?\s*[tq]\s*(iv|iii|ii|i|[1-4]))|(?:([1-4])\s*[tq]\s*[-–]?\s*(\d{4}))",
    re.I,
)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
# Sanity bounds: the statistic starts in the 1990s, and 4-digit *values* in a
# sheet (€/m² prices are 4-digit numbers) must never be read as years.
_YEAR_MIN, _YEAR_MAX = 1980, 2100


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
        year = int(plain_year.group(1))
        return dt.date(year, 1, 1) if _YEAR_MIN <= year <= _YEAR_MAX else None
    match = _QUARTER_RE.search(text)
    if not match:
        return None
    if match.group(1):
        year, q_raw = int(match.group(1)), match.group(2)
    else:
        year, q_raw = int(match.group(4)), match.group(3)
    if not _YEAR_MIN <= year <= _YEAR_MAX:
        return None
    quarter = _ROMAN[q_raw] if q_raw in _ROMAN else int(q_raw)
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


def _to_float(value: object) -> float | None:
    """A cell as a number, handling es-locale strings ("1.834,5"). Pure."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if "," in text:  # es-locale: dots are thousands separators
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def rows_from_frame(frame: pd.DataFrame) -> list[tuple[str, dt.date, float]]:
    """Parse a wide MIVAU frame into (region_code, period, value) rows. Pure.

    The region column is the non-period column with the most resolvable region
    names (sheets can carry blank filler columns before it). Each region name is
    mapped to every level it matches, so one sheet fills nation/community/
    province. Positional indexing throughout — repeated blank headers give
    duplicate column labels.
    """
    periods = [parse_period(str(c)) for c in frame.columns]
    region_idx, best_hits = None, 0
    for idx, period in enumerate(periods):
        if period is not None:
            continue
        hits = sum(1 for v in frame.iloc[:, idx] if region_codes_for_name(str(v)))
        if hits > best_hits:
            region_idx, best_hits = idx, hits
    if region_idx is None:
        return []
    rows: list[tuple[str, dt.date, float]] = []
    for _, record in frame.iterrows():
        codes = region_codes_for_name(str(record.iloc[region_idx]))
        if not codes:
            continue
        for idx, period in enumerate(periods):
            if period is None:
                continue
            numeric = _to_float(record.iloc[idx])
            if numeric is None:
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


def _read_raw(content: bytes, url: str, sheet: str | int = 0) -> pd.DataFrame | None:
    """Read a workbook into a header-less frame; ``.xls`` via xlrd, else openpyxl.

    Falls back to parsing an HTML table, since some ministry ``.XLS`` downloads
    are actually HTML. HTML numbers are es-locale ("1.834,5"); and read_html may
    promote ``<th>`` cells to column labels, so those are pushed back down as a
    row to keep the frame header-less like the Excel path.
    """
    engine = "xlrd" if url.lower().endswith(".xls") else "openpyxl"
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=sheet, header=None, engine=engine)
    except Exception:
        try:
            tables = pd.read_html(io.BytesIO(content), thousands=".", decimal=",")
        except Exception:
            return None
        if not tables:
            return None
        table = max(tables, key=lambda t: t.shape[0])
        header = pd.DataFrame([[str(c) for c in table.columns]])
        table.columns = range(len(table.columns))
        return pd.concat([header, table], ignore_index=True)


def ingest_spec(spec: MivauSpec) -> int:
    url = os.environ.get(spec.url_env, "").strip() or spec.default_url
    try:
        raw = _read_raw(_download(url), url, spec.sheet)
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
    written = upsert_observations(rows, SOURCE)
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
