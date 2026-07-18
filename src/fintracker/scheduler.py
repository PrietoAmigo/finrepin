"""APScheduler wiring: the app schedules itself, no external cron."""

from __future__ import annotations

import logging
from collections.abc import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from fintracker import heartbeat
from fintracker.config import Settings, get_settings
from fintracker.ingest.earnings import ingest_earnings_dates
from fintracker.ingest.fundamentals import ingest_fundamentals
from fintracker.ingest.market import ingest_market_data
from fintracker.ingest.ondemand import process_ticker_requests
from fintracker.ingest.yahoo_fundamentals import ingest_yahoo_fundamentals
from fintracker.report.email_report import send_weekly_report

log = logging.getLogger(__name__)

# If the container was asleep/restarting at fire time, still run within an hour.
_MISFIRE_GRACE_SECONDS = 3600


def _timezone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.tz)
    except Exception:
        log.warning("Invalid TZ %r — falling back to UTC.", settings.tz)
        return ZoneInfo("UTC")


def _guarded(name: str, fn: Callable[[], object]) -> Callable[[], None]:
    def runner() -> None:
        try:
            fn()
        except Exception:
            log.exception("Scheduled job %r failed", name)

    return runner


def build_scheduler() -> BlockingScheduler:
    settings = get_settings()
    tz = _timezone(settings)
    scheduler = BlockingScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "misfire_grace_time": _MISFIRE_GRACE_SECONDS},
    )

    scheduler.add_job(heartbeat.beat, IntervalTrigger(minutes=1, timezone=tz), id="heartbeat")
    # Dashboard search-box requests; minutely so a typed ticker lands fast.
    scheduler.add_job(
        _guarded("ticker-requests", process_ticker_requests),
        IntervalTrigger(minutes=1, timezone=tz),
        id="ticker-requests",
    )
    jobs: tuple[tuple[str, Callable[[], object], CronTrigger], ...] = (
        (
            "daily-market",
            ingest_market_data,
            CronTrigger(hour=settings.daily_hour, minute=settings.daily_minute, timezone=tz),
        ),
        (
            "daily-earnings",
            ingest_earnings_dates,
            CronTrigger(hour=settings.earnings_hour, minute=settings.earnings_minute, timezone=tz),
        ),
        (
            "daily-sec",
            ingest_fundamentals,
            CronTrigger(hour=settings.sec_hour, minute=settings.sec_minute, timezone=tz),
        ),
        (
            # Statements for listings without SEC coverage, same daily slot.
            "daily-yahoo-fundamentals",
            ingest_yahoo_fundamentals,
            CronTrigger(hour=settings.sec_hour, minute=settings.sec_minute, timezone=tz),
        ),
        (
            "weekly-email",
            send_weekly_report,
            CronTrigger(
                day_of_week=settings.weekly_day,
                hour=settings.weekly_hour,
                minute=settings.weekly_minute,
                timezone=tz,
            ),
        ),
    )
    for job_id, fn, trigger in jobs:
        scheduler.add_job(_guarded(job_id, fn), trigger, id=job_id)
        log.info("Scheduled %s: %s", job_id, trigger)
    return scheduler
