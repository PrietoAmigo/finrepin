"""Deterministic, clearly-labelled *sample* observations (``source='sample'``).

Lets the Grafana dashboard render before any live ingest — the numbers are
plausible but **not real**. Seeded only when ``HOUSING_SEED_SAMPLE=true`` and
only for indicators that have no data yet; the live ingestors clear sample rows
for the indicators they populate, so real data always supersedes it.
"""

from __future__ import annotations

import datetime as dt
import math

from fintracker.housing.regions import all_regions, regions_at
from fintracker.housing.store import Observation

SOURCE = "sample"

# A dozen notable municipalities so province → municipality drill-down has data.
_SAMPLE_MUNIS = [
    "muni-28079",  # Madrid
    "muni-08019",  # Barcelona
    "muni-46250",  # València
    "muni-41091",  # Sevilla
    "muni-50297",  # Zaragoza
    "muni-29067",  # Málaga
    "muni-30030",  # Murcia
    "muni-07040",  # Palma
    "muni-35016",  # Las Palmas de Gran Canaria
    "muni-48020",  # Bilbao
    "muni-03014",  # Alacant/Alicante
    "muni-14021",  # Córdoba
    "muni-15030",  # A Coruña
    "muni-18087",  # Granada
]

# National yearly anchors (€/m² for the all-housing price; other series scaled).
_PRICE_ANCHORS: dict[int, float] = {
    2007: 2050, 2008: 2020, 2009: 1920, 2010: 1830, 2011: 1700, 2012: 1580,
    2013: 1490, 2014: 1460, 2015: 1490, 2016: 1520, 2017: 1560, 2018: 1620,
    2019: 1660, 2020: 1655, 2021: 1690, 2022: 1770, 2023: 1835, 2024: 1935,
    2025: 2025, 2026: 2090,
}
_RENTA_ANCHORS: dict[int, float] = {
    2015: 11500, 2016: 11700, 2017: 12000, 2018: 12400, 2019: 12800,
    2020: 12600, 2021: 13100, 2022: 13600, 2023: 14100, 2024: 14600, 2025: 15000,
}

_PRICE_START = 2007
_ANNUAL_START = 2015
_END = dt.date(2026, 1, 1)


def _seed(code: str) -> int:
    return sum((i + 1) * ord(ch) for i, ch in enumerate(code))


def _factor(code: str, lo: float, hi: float) -> float:
    """Deterministic per-region multiplier in [lo, hi]."""
    frac = (_seed(code) % 1000) / 999.0
    return lo + (hi - lo) * frac


def _interp(anchors: dict[int, float], period: dt.date) -> float:
    """Linear interpolation between yearly anchors, clamped at both ends."""
    if period.year < min(anchors):
        return anchors[min(anchors)]
    if period.year > max(anchors):
        return anchors[max(anchors)]
    lo = anchors[period.year]
    hi = anchors.get(period.year + 1, lo)
    return lo + (hi - lo) * ((period.month - 1) / 12.0)


def _quarters(start_year: int) -> list[dt.date]:
    out, year = [], start_year
    while dt.date(year, 1, 1) <= _END:
        for month in (1, 4, 7, 10):
            day = dt.date(year, month, 1)
            if day <= _END:
                out.append(day)
        year += 1
    return out


def _years(start_year: int) -> list[dt.date]:
    return [dt.date(y, 1, 1) for y in range(start_year, _END.year + 1)]


def build_sample_observations() -> list[Observation]:
    """Sample rows for nation + every CCAA and province (+ a few municipalities)."""
    regions = [r for r in all_regions() if r.level in ("nation", "ccaa", "prov")]
    regions += [r for r in regions_at("muni") if r.code in set(_SAMPLE_MUNIS)]
    obs: list[Observation] = []

    def add(code: str, indicator: str, period: dt.date, value: float) -> None:
        obs.append((code, indicator, period, round(float(value), 2)))

    for region in regions:
        price_factor = _factor(region.code, 0.62, 1.9)
        wave_seed = _seed(region.code)

        # Prices — quarterly, €/m², four indicators.
        for period in _quarters(_PRICE_START):
            t = (period.year - _PRICE_START) * 4 + (period.month - 1) // 3
            wave = math.sin((t + wave_seed) / 3.3) * 18
            base = _interp(_PRICE_ANCHORS, period) * price_factor + wave
            add(region.code, "price_eur_m2", period, base)
            add(region.code, "price_eur_m2_new", period, base * 1.08)
            add(region.code, "price_eur_m2_used", period, base * 0.965)
            add(region.code, "price_eur_m2_protected", period, base * 0.72)  # VPO, cheaper

        # Income + demographics + housing stock — annual.
        area = _factor(region.code, 250, 21000) if region.level != "nation" else 505990
        pop_base = {
            "nation": 47_000_000, "ccaa": 3_000_000, "prov": 800_000, "muni": 250_000
        }[region.level] * _factor(region.code, 0.2, 3.2)
        for period in _years(_ANNUAL_START):
            renta = _interp(_RENTA_ANCHORS, period) * _factor(region.code, 0.78, 1.32)
            add(region.code, "renta_persona", period, renta)
            add(region.code, "renta_hogar", period, renta * 2.55)
            pop = pop_base * (1 + 0.004 * (period.year - _ANNUAL_START))
            add(region.code, "poblacion", period, pop)
            add(region.code, "superficie_km2", period, area)
            add(region.code, "densidad", period, pop / area)
            add(region.code, "viviendas_total", period, pop / 2.4)
            add(region.code, "viviendas_principales", period, pop / 2.5)
            add(region.code, "superficie_media_m2", period, _factor(region.code, 78, 108))
            add(region.code, "antiguedad_media", period, _factor(region.code, 28, 58))
    return obs
