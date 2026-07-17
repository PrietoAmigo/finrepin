"""Run Alembic migrations programmatically at boot."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

log = logging.getLogger(__name__)


def _find_alembic_ini() -> Path:
    # In the container alembic.ini sits in the workdir (/app); in a source
    # checkout it sits at the repo root, two levels above this file's package.
    candidates = (
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"alembic.ini not found in: {', '.join(map(str, candidates))}")


def run_migrations() -> None:
    ini = _find_alembic_ini()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    log.info("Running migrations (config: %s) ...", ini)
    command.upgrade(cfg, "head")
    log.info("Migrations are up to date.")
