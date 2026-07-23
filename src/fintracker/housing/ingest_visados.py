"""New-build permits (visados de dirección de obra) from MIVAU's Boletinonline.

The number of **dwellings** in new-construction permits is published by the
aparejadores' colleges (via MITMA/MIVAU) as one ``.XLS`` **per province** — the
older *Boletinonline* (v1) chapter 09, **table 21**: "Visados de dirección de
obra nueva. Nº de viviendas y superficie media según destino principal". The
sedal code is ``0903{PP}10.XLS`` where ``PP`` is the **INE province code**
(01 Álava … 28 Madrid … 50 Zaragoza; Ceuta/Melilla, 51/52, are not published).

Unlike the price files, each workbook is **transposed**: one province, a short
annual block followed by a long **monthly** history. The **year** runs down
column 0 (present only on the first month of each year), the Spanish **month**
abbreviation is in column 2, and the right-most value column is the **total
número de viviendas** (the sum of the destino-principal sub-columns). This reads
that monthly total, maps it to the province (from the sedal code), upserts it as
``visados``, then rolls the additive count up to CCAA + nation.

⚠️ 50 downloads per run — much heavier than the single-table INE series, and
written against one confirmed sample (Madrid), so a province that fails to
download or parse is logged and skipped rather than aborting the rest. Override
the sedal base with ``MIVAU_VISADOS_BASE_URL``.

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.ingest_visados
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Iterable

import pandas as pd

from fintracker.config import get_settings
from fintracker.housing.ingest_ine import derive_aggregates
from fintracker.housing.ingest_mivau import _download, _parse_year, _read_sheets, _to_float
from fintracker.housing.store import Observation, upsert_observations

log = logging.getLogger(__name__)

SOURCE = "MIVAU"
INDICATOR = "visados"
_SEDAL_V1 = "https://apps.fomento.gob.es/Boletinonline/sedal"
# Provincial table 21 files: 0903<PP>10.XLS, PP = INE province code 01..50.
_PROVINCE_CODES = range(1, 51)
# Data columns start at index 3 (0=year, 1=territory label, 2=month).
_FIRST_DATA_COL = 3

_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _month(value: object) -> int | None:
    """A cell as a Spanish month number ("Ene"→1 … "Dic"→12), else None. Pure."""
    if not isinstance(value, str):
        return None
    return _MONTHS.get(value.strip().lower()[:3])


def _total_viviendas(values: Iterable[object]) -> float | None:
    """The right-most whole, non-negative number in a row's data cells. Pure.

    The total número de viviendas is the last column; requiring a whole number
    skips the superficie-media columns (e.g. 227.04) even if the total is blank,
    so a mean area can never be mistaken for a dwelling count.
    """
    for value in reversed(list(values)):
        number = _to_float(value)
        if number is not None and number >= 0 and number == int(number):
            return number
    return None


def parse_visados_sheet(raw: pd.DataFrame) -> list[tuple[dt.date, float]]:
    """Parse one province's table-21 sheet into (period, value), oldest first. Pure.

    Reads only the monthly rows (those with a month in column 2); the year is
    carried from column 0 across a year's months. The annual summary block (no
    month) is ignored so its trailing year can't bleed into the monthly parse.
    """
    out: dict[dt.date, float] = {}
    current_year: int | None = None
    for _, row in raw.iterrows():
        cells = row.tolist()
        month = _month(cells[2]) if len(cells) > 2 else None
        if month is None:
            continue
        year = _parse_year(cells[0]) if cells else None
        if year is not None:
            current_year = year
        if current_year is None:
            continue
        total = _total_viviendas(cells[_FIRST_DATA_COL:])
        if total is None:
            continue
        out[dt.date(current_year, month, 1)] = total
    return sorted(out.items())


def _province_url(code: int, base: str) -> str:
    return f"{base}/0903{code:02d}10.XLS"


def ingest_province(code: int, base: str) -> int:
    """Fetch + parse one province's visados file and upsert its rows."""
    region = f"prov-{code:02d}"
    url = _province_url(code, base)
    try:
        sheets = _read_sheets(_download(url), url)
    except Exception:
        log.exception("Visados download failed for %s (%s)", region, url)
        return 0
    rows: list[Observation] = [
        (region, INDICATOR, period, value)
        for sheet in sheets
        for period, value in parse_visados_sheet(sheet)
    ]
    if not rows:
        log.warning("Parsed 0 visados rows for %s (%s)", region, url)
        return 0
    return upsert_observations(rows, SOURCE)


def ingest_visados() -> int:
    """Ingest visados for every province, then roll the count up to CCAA + nation."""
    base = os.environ.get("MIVAU_VISADOS_BASE_URL", "").strip() or _SEDAL_V1
    written = sum(ingest_province(code, base) for code in _PROVINCE_CODES)
    if written:
        derive_aggregates(INDICATOR)  # visados is additive: prov → ccaa → nation
        log.info("Ingested %d visados rows across %d provinces", written, len(_PROVINCE_CODES))
    return written


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_visados()
