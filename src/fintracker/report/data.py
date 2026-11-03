"""Queries that assemble the weekly report from the database."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fintracker.models import EarningsDate, Instrument, Price
from fintracker.portfolio import held_symbols


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
    # Suffix appended to the level for per-quantity prices ("/oz" for metals).
    unit: str = ""


@dataclass
class EarningsRow:
    symbol: str
    name: str
    date: dt.date
    is_estimated: bool
    # True for a holding, False for a watchlist name.
    held: bool = False


@dataclass
class Report:
    generated_at: dt.date
    lookback_days: int
    # What you own: always in the email, whatever the watchlist says.
    holdings: list[PriceRow] = field(default_factory=list)
    # What you're watching (never a holding — holdings are listed above).
    watchlist: list[PriceRow] = field(default_factory=list)
    # True when the watchlist is empty and the section shows the fallback list
    # (REPORT_SYMBOLS, else every tracked instrument you don't hold) instead.
    watchlist_is_fallback: bool = False
    earnings: list[EarningsRow] = field(default_factory=list)


_KIND_ORDER = {"equity": 0, "index": 1, "metal": 2, "crypto": 3, "forex": 4}

# Kinds the "every tracked instrument" fallback draws from: tradable prices,
# not the indexes, rates, and on-chain series that back the market dashboards.
_FALLBACK_KINDS = ("equity", "metal", "crypto", "forex")

# Level suffix per kind. Metals are quoted per troy ounce, so the level reads
# "$3,350.20/oz" while its weekly/monthly/yearly moves stay plain percentages.
_KIND_UNITS = {"metal": "/oz"}

# Symbols left out of the fallback list (an empty watchlist). Its crypto section
# focuses on Bitcoin (its price and MVRV Z-Score); ETH stays tracked and on the
# dashboards, and shows in the email when you hold it or watch it.
_EXCLUDED_SYMBOLS = frozenset({"ETH"})

# Enough daily bars to find a base price a year back (~260 trading days).
_HISTORY_ROWS = 400

# Bitcoin MVRV Z-Score: (market cap - realized cap) / stddev(market cap), with
# the standard deviation taken over the full stored history (so it self-calibrates
# as data grows, matching the Market Overview panel and lookintobitcoin.com).
# Realized cap is derived from the two Coin Metrics on-chain instruments as
# market cap / MVRV ratio (CapRealUSD needs a paid key; CapMVRVCur = market cap /
# realized cap is free, so this recovers realized cap exactly). The last
# `_HISTORY_ROWS` rows are enough to read the latest value and the period moves.
_MVRV_ZSCORE_SQL = text(
    """
    WITH mc AS (
        SELECT p.date, p.close::float8 AS market_cap
        FROM prices p JOIN instruments i ON i.id = p.instrument_id
        WHERE i.symbol = 'BTC-MCAP' AND p.close > 0
    ), mv AS (
        SELECT p.date, p.close::float8 AS mvrv
        FROM prices p JOIN instruments i ON i.id = p.instrument_id
        WHERE i.symbol = 'BTC-MVRV' AND p.close > 0
    ), s AS (SELECT stddev_pop(market_cap) AS sd FROM mc)
    SELECT mc.date AS date,
           (mc.market_cap - mc.market_cap / mv.mvrv) / NULLIF(s.sd, 0) AS zscore
    FROM mc JOIN mv ON mv.date = mc.date CROSS JOIN s
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
        unit=_KIND_UNITS.get(inst.kind, ""),
    )


def _price_moves(session: Session, lookback_days: int, symbols: set[str]) -> list[PriceRow]:
    """One row per symbol that has prices, sorted by kind then symbol."""
    if not symbols:
        return []
    instruments = (
        session.execute(
            select(Instrument)
            .where(Instrument.symbol.in_(sorted(symbols)))
            .order_by(Instrument.symbol)
        )
        .scalars()
        .all()
    )
    rows = [_price_row(session, inst, lookback_days) for inst in instruments]
    result = [r for r in rows if r is not None]
    # The BTC MVRV Z-Score rides alongside BTC, in whichever section BTC is in.
    if "BTC" in symbols:
        zscore = _mvrv_zscore_row(session, lookback_days)
        if zscore is not None:
            result.append(zscore)
    return sorted(result, key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.symbol))


def _watchlist_symbols(session: Session) -> set[str]:
    """Symbols flagged onto the weekly-email watchlist (Manage dashboard)."""
    return set(
        session.execute(select(Instrument.symbol).where(Instrument.in_watchlist))
        .scalars()
        .all()
    )


def _fallback_symbols(session: Session, report_symbols: tuple[str, ...]) -> set[str]:
    """What the Watchlist section shows while the watchlist is empty."""
    if report_symbols:
        symbols = set(report_symbols)
    else:
        symbols = set(
            session.execute(
                select(Instrument.symbol).where(Instrument.kind.in_(_FALLBACK_KINDS))
            )
            .scalars()
            .all()
        )
    return symbols - _EXCLUDED_SYMBOLS


def _upcoming_earnings(
    session: Session, today: dt.date, symbols: set[str], held: set[str]
) -> list[EarningsRow]:
    if not symbols:
        return []
    pairs = session.execute(
        select(EarningsDate, Instrument)
        .join(Instrument, Instrument.id == EarningsDate.instrument_id)
        .where(EarningsDate.earnings_date >= today, Instrument.symbol.in_(sorted(symbols)))
        .order_by(EarningsDate.earnings_date, Instrument.symbol)
    ).all()
    return [
        EarningsRow(
            symbol=inst.symbol,
            name=inst.name,
            date=ed.earnings_date,
            is_estimated=ed.is_estimated,
            held=inst.symbol in held,
        )
        for ed, inst in pairs
    ]


def build_report(session: Session, **overrides: Any) -> Report:
    from fintracker.config import get_settings

    settings = get_settings()
    lookback_days = int(overrides.get("lookback_days", settings.report_lookback_days))
    today: dt.date = overrides.get("today", dt.date.today())

    # Holdings are always in the email. The watchlist comes from an explicit
    # override (tests / manual runs), else the DB watchlist (edited from the
    # Manage dashboard), else — while that is empty — the REPORT_SYMBOLS env
    # seed, else every tracked stock/metal/crypto/FX pair. Whatever you hold is
    # listed under Holdings only, never twice.
    held = held_symbols(session)
    fallback = False
    if "symbols" in overrides:
        watched = {s.upper() for s in overrides["symbols"]}
    else:
        watched = _watchlist_symbols(session)
        if not watched:
            fallback = True
            watched = _fallback_symbols(session, settings.report_symbols)
    watched -= held

    return Report(
        generated_at=today,
        lookback_days=lookback_days,
        holdings=_price_moves(session, lookback_days, held),
        watchlist=_price_moves(session, lookback_days, watched),
        watchlist_is_fallback=fallback,
        earnings=_upcoming_earnings(session, today, held | watched, held),
    )
