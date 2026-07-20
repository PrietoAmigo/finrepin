"""Bundled, clearly-labelled *sample* housing dataset.

Lets the dashboard render immediately — before any live INE ingest and even
without a database — so the map and linked time series are explorable out of the
box. The numbers are illustrative (a plausible boom → bust → recovery shape,
base 2015=100), **not** real figures; the UI shows a "sample data" banner
whenever this is served. On the first successful INE ingest the live data takes
over automatically.

Regenerate the committed static copy (``web/sample-dataset.json``, used when the
page is opened as a bare file) with:
    python -m fintracker.housing.sample
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from fintracker.housing.dataset import SAMPLE_NOTE, Observation, assemble_dataset
from fintracker.housing.regions import REGIONS

# National IPV (general), base 2015=100 — yearly anchors tracing the real shape:
# the 2007 peak, the post-crisis trough around 2014, and the recovery since.
_NATIONAL_ANCHORS: dict[int, float] = {
    2007: 144.0,
    2008: 141.0,
    2009: 128.0,
    2010: 121.0,
    2011: 111.0,
    2012: 102.0,
    2013: 97.0,
    2014: 98.0,
    2015: 100.0,
    2016: 104.0,
    2017: 111.0,
    2018: 118.0,
    2019: 124.0,
    2020: 126.0,
    2021: 130.0,
    2022: 140.0,
    2023: 147.0,
    2024: 158.0,
    2025: 168.0,
    2026: 176.0,
}

# Per-CCAA level scaling (coastal/metro markets ran hotter than the interior).
_REGION_SCALE: dict[str, float] = {
    "ccaa-01": 0.98,  # Andalucía
    "ccaa-02": 0.94,  # Aragón
    "ccaa-03": 0.97,  # Asturias
    "ccaa-04": 1.12,  # Illes Balears
    "ccaa-05": 1.05,  # Canarias
    "ccaa-06": 0.98,  # Cantabria
    "ccaa-07": 0.90,  # Castilla y León
    "ccaa-08": 0.88,  # Castilla-La Mancha
    "ccaa-09": 1.06,  # Cataluña
    "ccaa-10": 0.99,  # Comunitat Valenciana
    "ccaa-11": 0.86,  # Extremadura
    "ccaa-12": 0.93,  # Galicia
    "ccaa-13": 1.14,  # Madrid
    "ccaa-14": 0.95,  # Murcia
    "ccaa-15": 1.02,  # Navarra
    "ccaa-16": 1.08,  # País Vasco
    "ccaa-17": 0.96,  # La Rioja
    "ccaa-18": 1.00,  # Ceuta
    "ccaa-19": 0.99,  # Melilla
}

# Components relative to the overall index (new-build a touch higher, resale a
# touch lower) — how INE's three IPV series typically sit against each other.
_COMPONENT_FACTOR: dict[str, float] = {
    "ipv_general": 1.000,
    "ipv_new": 1.020,
    "ipv_secondhand": 0.992,
}

_START_YEAR = 2007
_END = dt.date(2026, 1, 1)  # through 2026 Q1, fixed so the sample is stable


def _quarters() -> list[dt.date]:
    periods: list[dt.date] = []
    year = _START_YEAR
    while True:
        for month in (1, 4, 7, 10):
            day = dt.date(year, month, 1)
            if day > _END:
                return periods
            periods.append(day)
        year += 1


def _national_index(period: dt.date) -> float:
    """Linear interpolation of the yearly national anchors at a quarter."""
    lo = _NATIONAL_ANCHORS[period.year]
    hi = _NATIONAL_ANCHORS.get(period.year + 1, lo)
    frac = (period.month - 1) / 12.0
    return lo + (hi - lo) * frac


def _region_seed(region_code: str) -> int:
    return sum(ord(ch) for ch in region_code)


def build_sample_observations() -> list[Observation]:
    """Deterministic sample observations for the nation + every CCAA × component."""
    ccaa_codes = [r.code for r in REGIONS if r.level == "ccaa"]
    obs: list[Observation] = []
    for period in _quarters():
        national = _national_index(period)
        t = (period.year - _START_YEAR) * 4 + (period.month - 1) // 3
        for indicator, comp_factor in _COMPONENT_FACTOR.items():
            for region_code in ["es", *ccaa_codes]:
                scale = 1.0 if region_code == "es" else _REGION_SCALE[region_code]
                # Small deterministic per-region wave so curves aren't identical.
                wave = math.sin((t + _region_seed(region_code)) / 3.3) * 1.4
                value = round(national * scale * comp_factor + wave, 2)
                obs.append((indicator, region_code, period, value))
    return obs


def build_sample_dataset() -> dict[str, Any]:
    """The full sample payload, same shape as the live dataset."""
    return assemble_dataset(build_sample_observations(), mode="sample", note=SAMPLE_NOTE)


def _static_path() -> Path:
    # web/ lives at the repo root, four levels above this file.
    return Path(__file__).resolve().parents[3] / "web" / "sample-dataset.json"


if __name__ == "__main__":
    target = _static_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_sample_dataset(), ensure_ascii=False), encoding="utf-8")
    print(f"Wrote sample dataset to {target}")
