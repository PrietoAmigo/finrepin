"""Seed the region hierarchy + indicator registry, and (optionally) sample data.

``seed_housing`` runs at boot: reference data (regions, indicators) is always
upserted; clearly-labelled sample observations are inserted only when
``HOUSING_SEED_SAMPLE=true`` and only for indicators that have no rows yet, so
live data is never overwritten. The live ingestors call
``clear_sample_observations`` for the indicators they populate, so real data
supersedes any sample rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.indicators import INDICATORS
from fintracker.housing.regions import all_regions
from fintracker.housing.sample import SOURCE as SAMPLE_SOURCE
from fintracker.housing.sample import build_sample_observations
from fintracker.models import Indicator, Region, RegionObservation

log = logging.getLogger(__name__)

_CHUNK = 1000


def seed_regions() -> int:
    rows = [
        {
            "code": r.code,
            "level": r.level,
            "ine_code": r.ine,
            "name": r.name,
            "parent_code": r.parent,
            "lat": r.lat,
            "lon": r.lon,
        }
        for r in all_regions()
    ]
    with session_scope() as session:
        for offset in range(0, len(rows), _CHUNK):
            chunk = rows[offset : offset + _CHUNK]
            stmt = pg_insert(Region).values(chunk)
            update = {c: stmt.excluded[c] for c in chunk[0] if c != "code"}
            session.execute(stmt.on_conflict_do_update(index_elements=["code"], set_=update))
    return len(rows)


def seed_indicators() -> int:
    rows = [
        {
            "code": i.code,
            "name": i.name,
            "unit": i.unit,
            "source": i.source,
            "frequency": i.frequency,
            "category": i.category,
            "higher_is": i.higher_is,
        }
        for i in INDICATORS
    ]
    with session_scope() as session:
        stmt = pg_insert(Indicator).values(rows)
        update = {c: stmt.excluded[c] for c in rows[0] if c != "code"}
        session.execute(stmt.on_conflict_do_update(index_elements=["code"], set_=update))
    return len(rows)


def clear_sample_observations(indicators: Iterable[str]) -> int:
    """Delete sample rows for the given indicators (called by live ingestors)."""
    codes = list(indicators)
    if not codes:
        return 0
    with session_scope() as session:
        result = session.execute(
            delete(RegionObservation).where(
                RegionObservation.source == SAMPLE_SOURCE,
                RegionObservation.indicator.in_(codes),
            )
        )
    return int(getattr(result, "rowcount", 0) or 0)


def seed_sample_observations() -> int:
    """Insert sample rows for indicators that currently have no data."""
    with session_scope() as session:
        indicators_with_data = set(
            session.execute(
                select(RegionObservation.indicator).group_by(RegionObservation.indicator)
            ).scalars()
        )
        rows = [
            {
                "region_code": code,
                "indicator": indicator,
                "period": period,
                "value": value,
                "source": SAMPLE_SOURCE,
            }
            for code, indicator, period, value in build_sample_observations()
            if indicator not in indicators_with_data
        ]
        for offset in range(0, len(rows), _CHUNK):
            chunk = rows[offset : offset + _CHUNK]
            session.execute(
                pg_insert(RegionObservation)
                .values(chunk)
                .on_conflict_do_nothing(constraint="uq_region_obs_region_indicator_period")
            )
    return len(rows)


def seed_housing() -> None:
    """Boot-time seed: reference data always; sample data when enabled."""
    n_regions = seed_regions()
    n_indicators = seed_indicators()
    log.info("Seeded %d regions, %d indicators.", n_regions, n_indicators)
    if get_settings().housing_seed_sample:
        n_sample = seed_sample_observations()
        if n_sample:
            log.info("Seeded %d sample observations (HOUSING_SEED_SAMPLE=true).", n_sample)


def _observation_count() -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(RegionObservation)).scalar_one()


if __name__ == "__main__":
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    seed_housing()
    log.info("Total observations in DB: %d", _observation_count())
