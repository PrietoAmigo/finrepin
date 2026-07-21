"""Shared persistence for housing observations.

Both ingestors (INE, MIVAU) write ``(region, indicator, period, value)`` rows
through :func:`upsert_observations`, which dedupes on the unique key first —
sources routinely emit the same key twice (a MIVAU sheet lists "Madrid" as both
province and single-province community; an annual column and a T1 column map to
the same period date), and Postgres rejects an INSERT .. ON CONFLICT DO UPDATE
that touches one row twice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert

from fintracker.db import session_scope
from fintracker.models import RegionObservation

# One observation: (region_code, indicator, period, value).
Observation = tuple[str, str, dt.date, float]

_UPSERT_CHUNK = 500


def dedupe_observations(rows: Iterable[Observation]) -> list[Observation]:
    """Collapse duplicate (region, indicator, period) keys, last value wins. Pure.

    Last-wins matches column order in the sources: a quarterly column refines
    the annual column that precedes it.
    """
    by_key: dict[tuple[str, str, dt.date], float] = {}
    for region, indicator, period, value in rows:
        by_key[(region, indicator, period)] = value
    return [(r, i, p, v) for (r, i, p), v in by_key.items()]


def upsert_observations(rows: Iterable[Observation], source: str) -> int:
    """Upsert observations for ``source``; returns the number of distinct rows."""
    deduped = dedupe_observations(rows)
    with session_scope() as session:
        for offset in range(0, len(deduped), _UPSERT_CHUNK):
            chunk = deduped[offset : offset + _UPSERT_CHUNK]
            stmt = pg_insert(RegionObservation).values(
                [
                    {
                        "region_code": region,
                        "indicator": indicator,
                        "period": period,
                        "value": value,
                        "source": source,
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
    return len(deduped)
