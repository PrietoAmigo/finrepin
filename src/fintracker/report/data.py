"""Queries that assemble the weekly report from the database."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
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
    # A score row (e.g. the MVRV Z-Score) is unitless: its `level` and moves are
    # rendered as plain numbers/deltas, not a currency level and percentages.
    is_score: bool = False


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

# Symbols intentionally left out of the weekly email. The crypto section focuses
# on Bitcoin (its price and MVRV Z-Score); ETH stays tracked and on the
# dashboards, just not in the email.
_EXCLUDED_SYMBOLS = frozenset({"ETH"})

# Enough daily bars to find a base price a year back (~260 trading days).
_HISTORY_ROWS = 400

# Bitcoin MVRV Z-Score: (market cap - realized cap) / stddev(market cap), with
# the standard deviation taken over the full stored history (so it self-calibrates
# as data grows, matching the Market Overview panel and lookintobitcoin.com). The
# market cap and realized cap are the Coin Metrics on-chain instruments; the last
# `_HISTORY_ROWS` rows are enough to read the latest value and the period moves.
_MVRV_ZSCORE_SQL = text(
    """
    WITH mc AS (
        SELECT p.date, p.close::float8 AS market_cap
        FROM prices p JOIN instruments i ON i.id = p.instrument_id
        WHERE i.symbol = 'BTC-MCAP' AND p.close > 0
    ), rc AS (
        SELECT p.date, p.close::float8 AS realized_cap
        FROM prices p JOIN instruments i ON i.id = p.instrument_id
        WHERE i.symbol = 'BTC-RCAP'
    ), s AS (SELECT stddev_pop(market_cap) AS sd FROM mc)
    SELECT mc.date AS date,
           (mc.market_cap - rc.realized_cap) / NULLIF(s.sd, 0) AS zscore
    FROM mc JOIN rc ON rc.date = mc.date CROSS JOIN s
    ORDER BY mc.date DESC
    LIMIT :limit
    """
)


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


def change_abs(points: list[tuple[dt.date, float]], days: int) -> float | None:
    """Absolute move from the newest value vs the last value at least `days` older.

    For unitless series (the MVRV Z-Score) an absolute delta reads better than a
    percentage, which would blow up as the score crosses zero. `points` must be
    (date, value) pairs sorted newest-first.
    """
    if not points:
        return None
    latest_date, latest_val = points[0]
    cutoff = latest_date - dt.timedelta(days=days)
    base = next((val for (day, val) in points[1:] if day <= cutoff), None)
    if base is None:
        return None
    return latest_val - base


def _mvrv_zscore_row(session: Session, lookback_days: int) -> PriceRow | None:
    """The BTC MVRV Z-Score as a score row, or None if the inputs aren't ingested."""
    rows = session.execute(_MVRV_ZSCORE_SQL, {"limit": _HISTORY_ROWS}).mappings().all()
    points = [(r["date"], float(r["zscore"])) for r in rows if r["zscore"] is not None]
    if not points:
        return None
    return PriceRow(
        symbol="MVRV Z-Score",
        name="Bitcoin · market cap vs realized cap",
        kind="crypto",
        currency="",
        level=points[0][1],
        week_pct=change_abs(points, lookback_days),
        month_pct=change_abs(points, 30),
        year_pct=change_abs(points, 365),
        is_score=True,
    )


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
    rows = [
        _price_row(session, inst, lookback_days)
        for inst in instruments
        if inst.symbol not in _EXCLUDED_SYMBOLS
    ]
    result = [r for r in rows if r is not None]
    # The BTC MVRV Z-Score rides alongside BTC in the crypto section (shown
    # whenever BTC would be, i.e. no symbol allow-list or BTC is on it).
    if not symbols or "BTC" in symbols:
        zscore = _mvrv_zscore_row(session, lookback_days)
        if zscore is not None:
            result.append(zscore)
    return sorted(result, key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.symbol))


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
