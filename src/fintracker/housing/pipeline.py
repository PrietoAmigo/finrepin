"""Housing ingest orchestrator: INE regional series + MIVAU house prices.

Run one off-schedule ingest by hand with:
    python -m fintracker.housing.pipeline
"""

from __future__ import annotations

import logging

from fintracker.housing.ingest_censo import ingest_censo
from fintracker.housing.ingest_ine import ingest_ine
from fintracker.housing.ingest_mivau import ingest_mivau
from fintracker.housing.ingest_visados import ingest_visados
from fintracker.housing.territory import seed_territory_area

log = logging.getLogger(__name__)


def ingest_housing() -> None:
    """Run all housing ingestors; one source failing must not stop the others.

    Territory area runs first (a static reference series) so the density
    derivation at the end of the INE step has an area to divide population by.
    """
    totals: dict[str, int] = {}
    for name, ingestor in (
        ("territory", seed_territory_area),
        ("ine", ingest_ine),
        ("mivau", ingest_mivau),
        ("visados", ingest_visados),
        ("censo", ingest_censo),
    ):
        try:
            totals[name] = ingestor()
        except Exception:
            log.exception("Housing ingest step %r failed", name)
            totals[name] = 0
    log.info(
        "Housing ingest done: %s",
        ", ".join(f"{name}={count} rows" for name, count in totals.items()),
    )


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ingest_housing()
