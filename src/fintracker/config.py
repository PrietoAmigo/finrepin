"""Environment-driven configuration.

All knobs come from the environment (see `.env.example` for the full list).
Settings are read once and cached, so every module shares the same view.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote_plus


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # Timezone + scheduling
    tz: str
    daily_hour: int
    daily_minute: int
    earnings_hour: int
    earnings_minute: int
    sec_hour: int
    sec_minute: int
    weekly_day: str
    weekly_hour: int
    weekly_minute: int
    run_on_start: bool

    # Logging
    log_level: str

    # Email (weekly report)
    email_host: str
    email_port: int
    email_user: str
    email_pass: str
    email_to: str
    report_lookback_days: int
    grafana_url: str

    # SEC
    sec_user_agent: str

    # Healthcheck
    heartbeat_file: str

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def email_configured(self) -> bool:
        return bool(self.email_user and self.email_pass and self.email_to)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        db_host=_str("DB_HOST", "localhost"),
        db_port=_int("DB_PORT", 5432),
        db_name=_str("DB_NAME", "fintracker"),
        db_user=_str("DB_USER", "fintracker"),
        db_password=_str("DB_PASSWORD", ""),
        tz=_str("TZ", "UTC"),
        daily_hour=_int("DAILY_HOUR", 22),
        daily_minute=_int("DAILY_MINUTE", 30),
        earnings_hour=_int("EARNINGS_HOUR", 22),
        earnings_minute=_int("EARNINGS_MINUTE", 45),
        sec_hour=_int("SEC_HOUR", 23),
        sec_minute=_int("SEC_MINUTE", 0),
        weekly_day=_str("WEEKLY_DAY", "mon").lower(),
        weekly_hour=_int("WEEKLY_HOUR", 8),
        weekly_minute=_int("WEEKLY_MINUTE", 0),
        run_on_start=_bool("RUN_ON_START", True),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        email_host=_str("EMAIL_HOST", "smtp.gmail.com"),
        email_port=_int("EMAIL_PORT", 587),
        email_user=_str("EMAIL_USER"),
        email_pass=_str("EMAIL_PASS"),
        email_to=_str("EMAIL_TO"),
        report_lookback_days=_int("REPORT_LOOKBACK_DAYS", 7),
        grafana_url=_str("GRAFANA_URL", "http://localhost:3007"),
        sec_user_agent=_str("SEC_USER_AGENT"),
        heartbeat_file=_str("HEARTBEAT_FILE", "/tmp/fintracker-heartbeat"),
    )
