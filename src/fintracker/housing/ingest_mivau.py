"""House prices (€/m²) from the Ministerio de Vivienda (MIVAU / ex-Fomento).

The ministry publishes "Valor tasado de la vivienda" as legacy ``.XLS`` workbooks
(the "BoletinOnline" sedal files), not a JSON API. The real layout, confirmed
against the live files, is:

* **one sheet per four-year block** ("Tabla 1" = 1995-1998, "Tabla 2" =
  1999-2002, …) — so the whole history only appears if *every* sheet is read;
* a **two-row header** on each sheet — a *year* row (``Año 1995`` repeating at the
  first column of each four-column group) above a *quarter* row (``1º 2º 3º 4º``);
* **region names down one column**, ``€/m²`` values in the grid.

This ingest reads every sheet, reconstructs each data column's ``(year, quarter)``
period from the two header rows (the year is carried across its four quarter
columns), maps each region row to every level it matches (a "Madrid" row feeds
both province and community), and upserts. A single-header fallback
(``2024T1``-style labels or plain annual years) covers other layouts.

Defaults point at the current sedal files; override any with its ``MIVAU_*_URL``
env var.

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

# Single-cell period labels like "2024T1", "2024 T1", "2024-Q1", "2024TI",
# "1T2024", "2024" — used only by the single-header fallback. Roman numerals
# longest-first so "TIV" isn't read as "TI".
_QUARTER_RE = re.compile(
    r"(?:(\d{4})\s*[-–]?\s*[tq]\s*(iv|iii|ii|i|[1-4]))|(?:([1-4])\s*[tq]\s*[-–]?\s*(\d{4}))",
    re.I,
)
# A lone quarter marker in the two-row header: "1º", "2º", "IV", "T3", "4".
_QUARTER_ONLY_RE = re.compile(r"^\s*(?:t\s*)?(iv|iii|ii|i|[1-4])\s*(?:º|°|o|t|er|to|\.)?\s*$", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
# Sanity bounds: the statistic starts in the 1990s, and 4-digit *values* in a
# sheet (€/m² prices are 4-digit numbers) must never be read as years.
_YEAR_MIN, _YEAR_MAX = 1980, 2100


@dataclass(frozen=True)
class MivauSpec:
    indicator: str
    url_env: str  # env var holding the workbook URL (overrides default_url)
    default_url: str


MIVAU_SPECS: list[MivauSpec] = [
    MivauSpec("price_eur_m2", "MIVAU_PRICE_URL", f"{_SEDAL}/35101000.XLS"),
    MivauSpec("price_eur_m2_new", "MIVAU_PRICE_NEW_URL", f"{_SEDAL}/35101500.XLS"),
    MivauSpec("price_eur_m2_used", "MIVAU_PRICE_USED_URL", f"{_SEDAL}/35102000.XLS"),
    # 35102500 is protected housing (VPO) — a distinct series, not a re-appraisal
    # of the free-market price.
    MivauSpec("price_eur_m2_protected", "MIVAU_PROTECTED_URL", f"{_SEDAL}/35102500.XLS"),
    # Urban land price (€/m² de suelo) — a separate BoletinOnline2 workbook, same
    # €/m² grid shape, chapter 36 ("Estadística de precios de suelo urbano"),
    # table 4: "Precio medio del m² de suelo urbano por CCAA y provincias"
    # (sedal 36400500). Chapter 36 also holds land-transaction *count* tables
    # (36100500 etc.) whose values are not €/m², so the code is pinned to the
    # price table specifically. Override with MIVAU_SUELO_URL.
    MivauSpec("precio_suelo_m2", "MIVAU_SUELO_URL", f"{_SEDAL}/36400500.XLS"),
    # New-build permits (visados de dirección de obra) — same wide .XLS shape,
    # from the older Boletinonline (v1) chapter 09. This default was found via
    # search, not verified from here (host unreachable); confirm on the first
    # real run and override with MIVAU_VISADOS_URL if it 404s or parses 0 rows.
    MivauSpec("visados", "MIVAU_VISADOS_URL",
              "https://apps.fomento.gob.es/Boletinonline/sedal/09034720.XLS"),
]


def _parse_year(value: object) -> int | None:
    """A header cell as a 4-digit year in range ("Año 1995", 1995, "1995"). Pure."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, int | float):
        year = int(value)
        return year if _YEAR_MIN <= year <= _YEAR_MAX else None
    match = re.search(r"(\d{4})", str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if _YEAR_MIN <= year <= _YEAR_MAX else None


def _parse_quarter(value: object) -> int | None:
    """A header cell as a bare quarter 1-4 ("1º", "IV", "T3", "4"). Pure.

    Rejects anything that is not a lone quarter marker, so 4-digit years and
    €/m² price values are never mistaken for a quarter.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    match = _QUARTER_ONLY_RE.match(str(value).strip())
    if not match:
        return None
    token = match.group(1).lower()
    return _ROMAN[token] if token in _ROMAN else int(token)


def parse_period(label: str) -> dt.date | None:
    """Parse a single-cell MIVAU column header into a period start date. Pure."""
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


def detect_quarter_row(raw: pd.DataFrame) -> int | None:
    """Index of the header row that is a run of quarter markers ("1º 2º 3º 4º").

    The quarter row is unmistakable — no data row carries a line of ``1º``…``4º`` —
    so it anchors the two-row header. Pure.
    """
    best_row, best_hits = None, 0
    for i in range(min(len(raw), 30)):
        hits = sum(1 for v in raw.iloc[i].tolist() if _parse_quarter(v) is not None)
        if hits > best_hits:
            best_row, best_hits = i, hits
    return best_row if best_hits >= 4 else None


def column_periods(raw: pd.DataFrame, quarter_row: int) -> dict[int, dt.date]:
    """Map each data column index to its period, from the two-row header. Pure.

    The year row sits just above the quarter row and names a year only at the
    first column of each four-quarter group, so the year is carried forward
    across the group. A column gets a period only when it has both a carried
    year and a quarter marker — which excludes the label/region columns.
    """
    year_row = None
    for i in range(quarter_row - 1, max(-1, quarter_row - 5), -1):
        if any(_parse_year(v) is not None for v in raw.iloc[i].tolist()):
            year_row = i
            break
    if year_row is None:
        return {}
    years = raw.iloc[year_row].tolist()
    quarters = raw.iloc[quarter_row].tolist()
    periods: dict[int, dt.date] = {}
    current_year: int | None = None
    for col in range(raw.shape[1]):
        year = _parse_year(years[col]) if col < len(years) else None
        if year is not None:
            current_year = year
        quarter = _parse_quarter(quarters[col]) if col < len(quarters) else None
        if quarter is not None and current_year is not None:
            periods[col] = dt.date(current_year, (quarter - 1) * 3 + 1, 1)
    return periods


def _region_column(body: pd.DataFrame, period_cols: set[int]) -> int | None:
    """The non-period column whose cells resolve to the most region names. Pure."""
    region_idx, best_hits = None, 0
    for idx in range(body.shape[1]):
        if idx in period_cols:
            continue
        hits = sum(1 for v in body.iloc[:, idx] if region_codes_for_name(str(v)))
        if hits > best_hits:
            region_idx, best_hits = idx, hits
    return region_idx


def rows_from_sheet(raw: pd.DataFrame) -> list[tuple[str, dt.date, float]]:
    """Parse one header-less MIVAU sheet into (region_code, period, value). Pure.

    Uses the two-row (year + quarter) header when present, else falls back to a
    single header row of ``2024T1``-style / plain-year labels.
    """
    quarter_row = detect_quarter_row(raw)
    if quarter_row is None:
        return _rows_single_header(raw)
    periods = column_periods(raw, quarter_row)
    if len(periods) < 2:
        return _rows_single_header(raw)
    body = raw.iloc[quarter_row + 1 :]
    region_idx = _region_column(body, set(periods))
    if region_idx is None:
        return []
    rows: list[tuple[str, dt.date, float]] = []
    for _, record in body.iterrows():
        codes = region_codes_for_name(str(record.iloc[region_idx]))
        if not codes:
            continue
        for col, period in periods.items():
            value = _to_float(record.iloc[col])
            if value is None:
                continue
            for code in codes:
                rows.append((code, period, value))
    return rows


def detect_header_row(raw: pd.DataFrame) -> int | None:
    """Row index (0-based) with the most single-cell period labels. Pure.

    Fallback path for sheets whose header is one row of ``2024T1``/``2024`` labels.
    """
    best_row, best_hits = None, 0
    for i in range(min(len(raw), 25)):
        hits = sum(1 for v in raw.iloc[i].tolist() if parse_period(str(v)) is not None)
        if hits > best_hits:
            best_row, best_hits = i, hits
    return best_row if best_hits >= 2 else None


def _rows_single_header(raw: pd.DataFrame) -> list[tuple[str, dt.date, float]]:
    """Fallback parse: one header row of single-cell period labels. Pure."""
    header_row = detect_header_row(raw)
    if header_row is None:
        return []
    body = raw.iloc[header_row + 1 :]
    periods = {idx: p for idx, v in enumerate(raw.iloc[header_row].tolist())
               if (p := parse_period(str(v))) is not None}
    if not periods:
        return []
    region_idx = _region_column(body, set(periods))
    if region_idx is None:
        return []
    rows: list[tuple[str, dt.date, float]] = []
    for _, record in body.iterrows():
        codes = region_codes_for_name(str(record.iloc[region_idx]))
        if not codes:
            continue
        for col, period in periods.items():
            value = _to_float(record.iloc[col])
            if value is None:
                continue
            for code in codes:
                rows.append((code, period, value))
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


def _read_sheets(content: bytes, url: str) -> list[pd.DataFrame]:
    """Every sheet of a workbook as header-less frames; ``.xls`` via xlrd.

    Falls back to parsing HTML tables, since some ministry ``.XLS`` downloads are
    actually HTML. HTML numbers are es-locale ("1.834,5"); read_html may promote
    ``<th>`` cells to column labels, so those are pushed back down as a row to
    keep each frame header-less like the Excel path.
    """
    engine = "xlrd" if url.lower().endswith(".xls") else "openpyxl"
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine=engine)
        return list(sheets.values())
    except Exception:
        try:
            tables = pd.read_html(io.BytesIO(content), thousands=".", decimal=",")
        except Exception:
            return []
        out: list[pd.DataFrame] = []
        for table in tables:
            header = pd.DataFrame([[str(c) for c in table.columns]])
            table = table.copy()
            table.columns = range(len(table.columns))
            out.append(pd.concat([header, table], ignore_index=True))
        return out


def ingest_spec(spec: MivauSpec) -> int:
    url = os.environ.get(spec.url_env, "").strip() or spec.default_url
    if not url:
        log.info("MIVAU %s: no workbook URL — set %s to enable.", spec.indicator, spec.url_env)
        return 0
    try:
        sheets = _read_sheets(_download(url), url)
    except Exception:
        log.exception("MIVAU download failed for %s (%s)", spec.indicator, url)
        return 0
    if not sheets:
        log.warning("MIVAU: could not read any sheet for %s (%s)", spec.indicator, url)
        return 0
    parsed: list[tuple[str, dt.date, float]] = []
    for raw in sheets:
        parsed.extend(rows_from_sheet(raw))
    if not parsed:
        log.warning(
            "Parsed 0 MIVAU rows for %s from %s (%d sheet(s))", spec.indicator, url, len(sheets)
        )
        return 0
    rows = [(region, spec.indicator, period, value) for region, period, value in parsed]
    written = upsert_observations(rows, SOURCE)
    log.info(
        "Ingested %d MIVAU rows for %s (%d regions, %d sheets)",
        written, spec.indicator, len({r[0] for r in rows}), len(sheets),
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
