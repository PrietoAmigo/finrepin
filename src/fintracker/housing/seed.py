"""Seed the region hierarchy + indicator registry.

``seed_housing`` runs at boot and upserts the reference data (regions,
indicators) that the ingestors and dashboard depend on. Observations come only
from the live INE/MIVAU ingestors — no data is fabricated here.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.housing.indicators import INDICATORS
from fintracker.housing.regions import all_regions
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


def seed_housing() -> None:
    """Boot-time seed of the region + indicator reference data."""
    n_regions = seed_regions()
    n_indicators = seed_indicators()
    log.info("Seeded %d regions, %d indicators.", n_regions, n_indicators)


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
