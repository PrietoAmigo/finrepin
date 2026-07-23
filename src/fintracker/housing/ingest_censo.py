"""Census housing characteristics from INE's PC-Axis (``.px``) tables.

The *Censo de Población y Viviendas 2021* publishes dwelling characteristics —
counts by type, mean floor area, year of construction — only as ``.px`` files
(see :mod:`fintracker.housing.pcaxis`), not the Tempus3 JSON the other INE
series use. This module fetches a configured ``.px``, resolves its territory
dimension to region codes, and either stores a selected value directly (dwelling
counts) or computes a **weighted mean from a bucketed distribution** (mean floor
area from the surface-band table, mean dwelling age from the year-of-construction
table).

Every spec is **off by default and pinned by an env URL** — never guessed — so a
census series stays empty until its ``.px`` URL is set. The dimension/category
names below are best-effort and easy to adjust once a real ``.px`` is inspected
on a host that can reach INE (CI can't): dump one with, e.g.::

    import requests
    from fintracker.housing.pcaxis import parse_px
    t = parse_px(requests.get("<px url>").text)
    print(t.dims); print({d: t.categories[d][:6] for d in t.dims})

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.ingest_censo
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings
from fintracker.housing.pcaxis import PxTable, parse_px
from fintracker.housing.regions import region_code_from_ine_code, region_code_from_ine_name
from fintracker.housing.store import upsert_observations

log = logging.getLogger(__name__)

SOURCE = "INE-censo"
_TIMEOUT = (10, 120)
_CODE_RE = re.compile(r"\b(\d{2,5})\b")


@dataclass(frozen=True)
class CensoSpec:
    indicator: str
    url_env: str  # env var holding the .px URL (overrides default_url)
    level: str = "prov"  # ccaa | prov | muni
    default_url: str = ""  # built-in .px URL; empty ⇒ off until the env var is set
    period: dt.date = dt.date(2021, 1, 1)  # census reference date
    # Fix one or more dimensions to a category before reading (e.g. type=Total).
    select: tuple[tuple[str, str], ...] = ()
    # Weighted-mean mode: spread the count across `bucket_dim`, weighting each
    # category by the midpoint whose substring it contains.
    bucket_dim: str = ""
    midpoints: tuple[tuple[str, float], ...] = ()


# Dwelling COUNTS come from the JSON table 3457 (see ingest_ine). Mean floor area
# and mean dwelling age both come from ONE census .px — "Número de viviendas
# principales por provincias según año de construcción y superficie útil"
# (t20/p274/serie/def/p09/l0/03004.px): Provincias × Año de construcción ×
# periodo × Superficie útil. Fix periodo + the other characteristic to Total, then
# take a weighted mean over the remaining bands. Latest periodo is 2020.
_PX_VIVIENDAS = (
    "https://www.ine.es/jaxi/files/_px/es/px/t20/p274/serie/def/p09/l0/03004.px"
)
_PERIODO = "2020"

CENSO_SPECS: list[CensoSpec] = [
    # Mean floor area: fix año=Total, weighted-mean over the surface bands.
    CensoSpec(
        "superficie_media_m2", "CENSO_SUPERFICIE_PX_URL", "prov",
        default_url=_PX_VIVIENDAS, period=dt.date(int(_PERIODO), 1, 1),
        select=(("Año de construcción", "Total"), ("periodo", _PERIODO)),
        bucket_dim="Superficie útil",
        midpoints=(
            ("menos de 46", 38.0), ("46 y 75", 60.0), ("76 y 105", 90.0),
            ("106 y 150", 128.0), ("de 150", 185.0),
        ),
    ),
    # Mean dwelling age: fix superficie=Total, weighted-mean over construction-year
    # bands, each midpoint expressed as age = periodo − mid-year.
    CensoSpec(
        "antiguedad_media", "CENSO_ANTIGUEDAD_PX_URL", "prov",
        default_url=_PX_VIVIENDAS, period=dt.date(int(_PERIODO), 1, 1),
        select=(("Superficie útil", "Total"), ("periodo", _PERIODO)),
        bucket_dim="Año de construcción",
        midpoints=(
            ("posterior al 2010", 5.0), ("2006 y 2010", 12.0), ("2001 y 2005", 17.0),
            ("1991 y 2000", 25.0), ("1981 y 1990", 35.0), ("1971 y 1980", 45.0),
            ("1961 y 1970", 55.0), ("1951 y 1960", 65.0), ("1941 y 1950", 75.0),
            ("1921 y 1940", 90.0), ("antes de 1921", 110.0),
        ),
    ),
]


def weighted_mean_from_buckets(
    counts: list[tuple[str, float | None]], midpoints: tuple[tuple[str, float], ...]
) -> float | None:
    """Weighted mean of bucket midpoints, weights = counts. Pure.

    Each ``(category_label, count)`` is matched to the first midpoint whose
    (normalised) substring appears in the label; unmatched or empty buckets are
    ignored. Returns ``None`` when no weight lands.
    """
    total = 0.0
    weight = 0.0
    for label, count in counts:
        if not count or count <= 0:
            continue
        low = label.lower()
        for needle, mid in midpoints:
            if needle.lower() in low:
                total += count * mid
                weight += count
                break
    return total / weight if weight > 0 else None


def _region_for(label: str, level: str) -> str | None:
    """Resolve a PC-Axis territory label to a region code. Pure.

    Census labels lead with the INE code (``"28 Madrid"``, ``"28079 Madrid"``);
    fall back to name matching for CCAA/province when no code is present.
    """
    match = _CODE_RE.match(label.strip())
    if match:
        code = region_code_from_ine_code(match.group(1), level)
        if code:
            return code
    return region_code_from_ine_name(label, level) if level in ("ccaa", "prov") else None


def _territory_dim(table: PxTable, level: str) -> str | None:
    """The dimension whose categories resolve to the most region codes. Pure."""
    best_dim, best_hits = None, 0
    for dim in table.dims:
        hits = sum(1 for cat in table.categories[dim] if _region_for(cat, level))
        if hits > best_hits:
            best_dim, best_hits = dim, hits
    return best_dim


def rows_from_px(table: PxTable, spec: CensoSpec) -> list[tuple[str, dt.date, float]]:
    """Map a parsed ``.px`` to (region_code, period, value) rows for ``spec``. Pure."""
    cells = table.cells
    for dim, category in spec.select:
        if dim not in table.dims:
            return []
        i = table.dims.index(dim)
        cells = [(labels, v) for labels, v in cells if labels[i] == category]
    terr_dim = _territory_dim(table, spec.level)
    if terr_dim is None:
        return []
    ti = table.dims.index(terr_dim)

    if spec.bucket_dim:
        if spec.bucket_dim not in table.dims:
            return []
        bi = table.dims.index(spec.bucket_dim)
        grouped: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
        for labels, value in cells:
            grouped[labels[ti]].append((labels[bi], value))
        out: list[tuple[str, dt.date, float]] = []
        for territory, pairs in grouped.items():
            code = _region_for(territory, spec.level)
            mean = weighted_mean_from_buckets(pairs, spec.midpoints)
            if code and mean is not None:
                out.append((code, spec.period, mean))
        return out

    out = []
    for labels, value in cells:
        if value is None:
            continue
        code = _region_for(labels[ti], spec.level)
        if code:
            out.append((code, spec.period, float(value)))
    return out


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, max=30),
    reraise=True,
)
def fetch_px(url: str) -> str:
    resp = requests.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "latin-1"  # INE .px are ISO-8859-1/15
    return resp.text


def ingest_spec(spec: CensoSpec) -> int:
    url = os.environ.get(spec.url_env, "").strip() or spec.default_url
    if not url:
        log.info("Censo %s: no .px URL — set %s to enable.", spec.indicator, spec.url_env)
        return 0
    try:
        table = parse_px(fetch_px(url))
    except Exception:
        log.exception("Censo fetch/parse failed for %s (%s)", spec.indicator, url)
        return 0
    parsed = rows_from_px(table, spec)
    if not parsed:
        log.warning("Parsed 0 censo rows for %s from %s", spec.indicator, url)
        return 0
    rows = [(region, spec.indicator, period, value) for region, period, value in parsed]
    written = upsert_observations(rows, SOURCE)
    log.info("Ingested %d censo rows for %s (%d regions)",
             written, spec.indicator, len({r[0] for r in rows}))
    return written


def ingest_censo() -> int:
    return sum(ingest_spec(spec) for spec in CENSO_SPECS)


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_censo()
