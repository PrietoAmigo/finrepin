"""Engine/session management and startup wait-for-db."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_delay, wait_fixed

from fintracker.config import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction per unit of work: commit on success, rollback on error."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wait_for_db(timeout_seconds: int = 120) -> None:
    """Block until Postgres accepts connections (Compose may start us first)."""

    @retry(stop=stop_after_delay(timeout_seconds), wait=wait_fixed(2), reraise=True)
    def _ping() -> None:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))

    log.info("Waiting for database at %s:%s ...", get_settings().db_host, get_settings().db_port)
    _ping()
    log.info("Database is up.")
