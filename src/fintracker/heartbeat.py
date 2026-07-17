"""Scheduler heartbeat: a timestamp file the Docker healthcheck watches."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fintracker.config import get_settings

log = logging.getLogger(__name__)


def beat() -> None:
    path = Path(get_settings().heartbeat_file)
    try:
        path.write_text(str(int(time.time())))
    except OSError:
        log.exception("Could not write heartbeat file %s", path)
