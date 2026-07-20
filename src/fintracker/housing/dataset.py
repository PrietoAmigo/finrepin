"""Shape housing observations into the compact JSON the dashboard consumes.

``assemble_dataset`` is the single shared builder — both the live DB query and
the bundled sample feed it, so their payloads are byte-for-byte the same shape.
The front-end does all cross-filtering (map ↔ time series) client-side from this
one payload, so it's returned whole rather than as many small endpoints.

Payload::

    {
      "mode": "live" | "sample",
      "generated_at": "<iso8601>",
      "updated_at": "YYYY-MM-DD" | null,   # latest period present
      "source_note": "<html-safe note>",
      "levels": ["ccaa"],                  # geography levels present
      "indicators": [ {code,name,description,unit,source,base,frequency,higher_is}, ...],
      "nation": {"code": "es", "name": "España"},
      "regions": {"ccaa": [ {code,name,parent}, ... ]},
      "periods": { "<indicator>": ["YYYY-MM-DD", ...] },
      "series":  { "<indicator>": { "<region_code>": [value|null, ...] } }
    }
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.housing.regions import (
    INDICATORS,
    REGIONS,
    REGIONS_BY_CODE,
)
from fintracker.models import HousingPrice

# One observation: (indicator_code, region_code, period, value).
Observation = tuple[str, str, dt.date, float]

LIVE_NOTE = "Live data from INE (Instituto Nacional de Estadística), House Price Index (IPV)."
SAMPLE_NOTE = (
    "Illustrative sample data — not real figures. Run the INE ingest "
    "(python -m fintracker.housing.ingest) to load live House Price Index data."
)

_LEVEL_ORDER = {"nation": 0, "ccaa": 1, "prov": 2}


def assemble_dataset(observations: Iterable[Observation], mode: str, note: str) -> dict[str, Any]:
    """Build the dashboard payload from a flat iterable of observations."""
    # indicator -> sorted period list; (indicator, region) -> {period: value}
    periods_by_ind: dict[str, set[dt.date]] = {}
    values: dict[tuple[str, str], dict[dt.date, float]] = {}
    present_regions: set[str] = set()
    present_indicators: set[str] = set()
    latest: dt.date | None = None

    for indicator, region, period, value in observations:
        if region not in REGIONS_BY_CODE:
            continue
        periods_by_ind.setdefault(indicator, set()).add(period)
        values.setdefault((indicator, region), {})[period] = value
        present_regions.add(region)
        present_indicators.add(indicator)
        latest = period if latest is None else max(latest, period)

    sorted_periods = {ind: sorted(ps) for ind, ps in periods_by_ind.items()}

    indicators = [asdict(i) for i in INDICATORS if i.code in present_indicators]

    series: dict[str, dict[str, list[float | None]]] = {}
    for indicator in (i["code"] for i in indicators):
        periods = sorted_periods[indicator]
        per_indicator: dict[str, list[float | None]] = {}
        for region in present_regions:
            cell = values.get((indicator, region))
            if not cell:
                continue
            per_indicator[region] = [cell.get(p) for p in periods]
        series[indicator] = per_indicator

    # Levels present, ordered nation -> ccaa -> prov.
    levels = sorted(
        {REGIONS_BY_CODE[c].level for c in present_regions if REGIONS_BY_CODE[c].level != "nation"},
        key=lambda lvl: _LEVEL_ORDER.get(lvl, 9),
    )
    regions_by_level: dict[str, list[dict[str, Any]]] = {}
    for reg in REGIONS:  # registry order
        if reg.code not in present_regions or reg.level == "nation":
            continue
        regions_by_level.setdefault(reg.level, []).append(
            {"code": reg.code, "name": reg.name, "parent": reg.parent}
        )

    nation = REGIONS_BY_CODE["es"]
    return {
        "mode": mode,
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds"),
        "updated_at": latest.isoformat() if latest else None,
        "source_note": note,
        "levels": levels,
        "indicators": indicators,
        "nation": {"code": nation.code, "name": nation.name},
        "regions": regions_by_level,
        "periods": {ind: [p.isoformat() for p in ps] for ind, ps in sorted_periods.items()},
        "series": series,
    }


def _live_observations(session: Session) -> list[Observation]:
    rows = session.execute(
        select(
            HousingPrice.indicator,
            HousingPrice.region_code,
            HousingPrice.period,
            HousingPrice.value,
        )
    ).all()
    return [(r.indicator, r.region_code, r.period, float(r.value)) for r in rows]


def build_live_dataset(session: Session) -> dict[str, Any] | None:
    """Assemble the payload from the database, or None if no rows are stored."""
    observations = _live_observations(session)
    if not observations:
        return None
    return assemble_dataset(observations, mode="live", note=LIVE_NOTE)


def build_dataset() -> dict[str, Any]:
    """Live dataset if the DB has housing rows, else the bundled sample.

    Never raises for a missing/empty database — the dashboard always renders
    something (falling back to clearly-labelled sample data).
    """
    from fintracker.db import session_scope
    from fintracker.housing.sample import build_sample_dataset

    try:
        with session_scope() as session:
            live = build_live_dataset(session)
        if live is not None:
            return live
    except Exception:  # pragma: no cover - DB optional for the sample fallback
        pass
    return build_sample_dataset()
