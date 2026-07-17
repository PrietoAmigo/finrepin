"""Docker healthcheck: exit 0 while the scheduler heartbeat stays fresh."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from fintracker.config import get_settings

MAX_AGE_SECONDS = 180  # heartbeat is written every minute; allow two misses


def main() -> int:
    path = Path(get_settings().heartbeat_file)
    try:
        beat_at = int(path.read_text().strip())
    except (OSError, ValueError):
        return 1
    return 0 if time.time() - beat_at <= MAX_AGE_SECONDS else 1


if __name__ == "__main__":
    sys.exit(main())
