"""Container entrypoint: wait for DB, migrate, seed, optionally ingest, schedule."""

from __future__ import annotations

import logging

from fintracker import __version__, heartbeat
from fintracker.config import get_settings
from fintracker.db import wait_for_db
from fintracker.housing.pipeline import ingest_housing
from fintracker.housing.seed import seed_housing
from fintracker.ingest.market import ingest_market_data
from fintracker.migrate import run_migrations
from fintracker.scheduler import build_scheduler
from fintracker.seed import seed_instruments

log = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log.info("fintracker %s starting (tz=%s)", __version__, settings.tz)
    wait_for_db()
    run_migrations()
    seed_instruments()
    seed_housing()  # regions + indicators (+ sample data when enabled)
    heartbeat.beat()  # pass the healthcheck while the first ingest runs

    if settings.run_on_start:
        log.info("RUN_ON_START=true — running one market ingest now.")
        try:
            ingest_market_data()
        except Exception:
            log.exception("Initial market ingest failed; the scheduled run will retry.")
        log.info("RUN_ON_START=true — running one housing ingest now.")
        try:
            ingest_housing()
        except Exception:
            log.exception("Initial housing ingest failed; the scheduled run will retry.")

    scheduler = build_scheduler()
    log.info("Entering scheduler loop.")
    scheduler.start()


if __name__ == "__main__":
    main()
