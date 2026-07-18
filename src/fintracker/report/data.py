"""Queries that assemble the weekly report from the database."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.models import EarningsDate, Instrument, Price


@dataclass
class PriceRow:
    symbol: str
    name: str
    kind: str
    currency: str
    level: float
    week_pct: float | None
    month_pct: float | None
    year_pct: float | None


@dataclass
class EarningsRow:
    symbol: str
    name: str
    date: dt.date
    is_estimated: bool


@dataclass
class Report:
    generated_at: dt.date
    lookback_days: int
    prices: list[PriceRow] = field(default_factory=list)
    earnings: list[EarningsRow] = field(default_factory=list)


_KIND_ORDER = {"equity": 0, "crypto": 1, "forex": 2}

# Enough daily bars to find a base price a year back (~260 trading days).
_HISTORY_ROWS = 400


def _pct(current: float, base: float) -> float | None:
    if base == 0:
        return None
    return (current / base - 1.0) * 100.0


def change_pct(prices: list[Price], days: int) -> float | None:
    """% move from the newest close vs the last close at least `days` older.

    `prices` must be sorted newest-first.
    """
    if not prices:
        return None
    latest = prices[0]
    cutoff = latest.date - dt.timedelta(days=days)
    base = next((p for p in prices[1:] if p.date <= cutoff), None)
    if base is None:
        return None
    return _pct(float(latest.close), float(base.close))


def _price_row(session: Session, inst: Instrument, lookback_days: int) -> PriceRow | None:
    prices = (
        session.execute(
            select(Price)
            .where(Price.instrument_id == inst.id)
            .order_by(Price.date.desc())
            .limit(_HISTORY_ROWS)
        )
        .scalars()
        .all()
    )
    if not prices:
        return None
    return PriceRow(
        symbol=inst.symbol,
        name=inst.name,
        kind=inst.kind,
        currency=inst.currency,
        level=float(prices[0].close),
        week_pct=change_pct(list(prices), lookback_days),
        month_pct=change_pct(list(prices), 30),
        year_pct=change_pct(list(prices), 365),
    )


def _price_moves(
    session: Session, lookback_days: int, symbols: tuple[str, ...]
) -> list[PriceRow]:
    query = select(Instrument).order_by(Instrument.symbol)
    if symbols:
        query = query.where(Instrument.symbol.in_(symbols))
    instruments = session.execute(query).scalars().all()
    rows = [_price_row(session, inst, lookback_days) for inst in instruments]
    return sorted(
        (r for r in rows if r is not None), key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.symbol)
    )


def _upcoming_earnings(
    session: Session, today: dt.date, symbols: tuple[str, ...]
) -> list[EarningsRow]:
    query = (
        select(EarningsDate, Instrument)
        .join(Instrument, Instrument.id == EarningsDate.instrument_id)
        .where(EarningsDate.earnings_date >= today)
        .order_by(EarningsDate.earnings_date)
    )
    if symbols:
        query = query.where(Instrument.symbol.in_(symbols))
    pairs = session.execute(query).all()
    return [
        EarningsRow(
            symbol=inst.symbol, name=inst.name, date=ed.earnings_date, is_estimated=ed.is_estimated
        )
        for ed, inst in pairs
    ]


def build_report(session: Session, **overrides: Any) -> Report:
    from fintracker.config import get_settings

    settings = get_settings()
    lookback_days = int(overrides.get("lookback_days", settings.report_lookback_days))
    symbols: tuple[str, ...] = tuple(overrides.get("symbols", settings.report_symbols))
    today: dt.date = overrides.get("today", dt.date.today())

    return Report(
        generated_at=today,
        lookback_days=lookback_days,
        prices=_price_moves(session, lookback_days, symbols),
        earnings=_upcoming_earnings(session, today, symbols),
    )
