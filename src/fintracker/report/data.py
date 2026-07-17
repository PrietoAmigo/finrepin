"""Queries that assemble the weekly report from the database."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.models import EarningsDate, Filing, Fundamental, Instrument, Price


@dataclass
class PriceRow:
    symbol: str
    name: str
    kind: str
    currency: str
    level: float
    day_pct: float | None
    week_pct: float | None


@dataclass
class EarningsRow:
    symbol: str
    name: str
    date: dt.date
    is_estimated: bool


@dataclass
class FactHighlight:
    label: str
    value: float
    unit: str


@dataclass
class FilingRow:
    symbol: str
    form: str
    filed_at: dt.date
    period_end: dt.date | None
    facts: list[FactHighlight] = field(default_factory=list)


@dataclass
class Report:
    generated_at: dt.date
    lookback_days: int
    grafana_url: str
    prices: list[PriceRow] = field(default_factory=list)
    earnings: list[EarningsRow] = field(default_factory=list)
    filings: list[FilingRow] = field(default_factory=list)


# Priority-ordered (tag -> display label); the first fact found per label wins.
DISPLAY_TAGS: tuple[tuple[str, str], ...] = (
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue"),
    ("Revenues", "Revenue"),
    ("Revenue", "Revenue"),
    ("NetIncomeLoss", "Net income"),
    ("ProfitLossAttributableToOwnersOfParent", "Net income"),
    ("ProfitLoss", "Net income"),
    ("EarningsPerShareDiluted", "Diluted EPS"),
    ("Assets", "Total assets"),
)

_KIND_ORDER = {"equity": 0, "crypto": 1, "forex": 2}


def _pct(current: float, base: float) -> float | None:
    if base == 0:
        return None
    return (current / base - 1.0) * 100.0


def _price_row(session: Session, inst: Instrument, lookback_days: int) -> PriceRow | None:
    prices = (
        session.execute(
            select(Price)
            .where(Price.instrument_id == inst.id)
            .order_by(Price.date.desc())
            .limit(lookback_days + 15)
        )
        .scalars()
        .all()
    )
    if not prices:
        return None
    latest = prices[0]
    level = float(latest.close)
    day_pct = _pct(level, float(prices[1].close)) if len(prices) > 1 else None
    week_cutoff = latest.date - dt.timedelta(days=lookback_days)
    week_base = next((p for p in prices[1:] if p.date <= week_cutoff), None)
    week_pct = _pct(level, float(week_base.close)) if week_base else None
    return PriceRow(
        symbol=inst.symbol,
        name=inst.name,
        kind=inst.kind,
        currency=inst.currency,
        level=level,
        day_pct=day_pct,
        week_pct=week_pct,
    )


def _price_moves(session: Session, lookback_days: int) -> list[PriceRow]:
    instruments = session.execute(select(Instrument).order_by(Instrument.symbol)).scalars().all()
    rows = [_price_row(session, inst, lookback_days) for inst in instruments]
    return sorted(
        (r for r in rows if r is not None), key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.symbol)
    )


def _upcoming_earnings(session: Session, today: dt.date) -> list[EarningsRow]:
    pairs = session.execute(
        select(EarningsDate, Instrument)
        .join(Instrument, Instrument.id == EarningsDate.instrument_id)
        .where(EarningsDate.earnings_date >= today)
        .order_by(EarningsDate.earnings_date)
    ).all()
    return [
        EarningsRow(
            symbol=inst.symbol, name=inst.name, date=ed.earnings_date, is_estimated=ed.is_estimated
        )
        for ed, inst in pairs
    ]


def _facts_for_filing(session: Session, filing: Filing) -> list[FactHighlight]:
    facts = (
        session.execute(
            select(Fundamental).where(
                Fundamental.instrument_id == filing.instrument_id,
                Fundamental.accession_no == filing.accession_no,
            )
        )
        .scalars()
        .all()
    )
    by_tag: dict[str, list[Fundamental]] = {}
    for fact in facts:
        by_tag.setdefault(fact.tag, []).append(fact)

    highlights: list[FactHighlight] = []
    seen_labels: set[str] = set()
    for tag, label in DISPLAY_TAGS:
        if label in seen_labels or tag not in by_tag:
            continue
        best = max(by_tag[tag], key=lambda f: (f.period_end, f.filed_at or dt.date.min))
        highlights.append(FactHighlight(label=label, value=float(best.value), unit=best.unit))
        seen_labels.add(label)
    return highlights


def _new_filings(session: Session, today: dt.date, lookback_days: int) -> list[FilingRow]:
    cutoff = today - dt.timedelta(days=lookback_days)
    pairs = session.execute(
        select(Filing, Instrument)
        .join(Instrument, Instrument.id == Filing.instrument_id)
        .where(Filing.filed_at >= cutoff)
        .order_by(Filing.filed_at.desc())
    ).all()

    rows: list[FilingRow] = []
    for filing, inst in pairs:
        facts = (
            session.execute(
                select(Fundamental.period_end).where(
                    Fundamental.instrument_id == filing.instrument_id,
                    Fundamental.accession_no == filing.accession_no,
                )
            )
            .scalars()
            .all()
        )
        rows.append(
            FilingRow(
                symbol=inst.symbol,
                form=filing.form,
                filed_at=filing.filed_at,
                period_end=max(facts) if facts else None,
                facts=_facts_for_filing(session, filing),
            )
        )
    return rows


def build_report(session: Session, **overrides: Any) -> Report:
    from fintracker.config import get_settings

    settings = get_settings()
    lookback_days = int(overrides.get("lookback_days", settings.report_lookback_days))
    grafana_url = str(overrides.get("grafana_url", settings.grafana_url))
    today: dt.date = overrides.get("today", dt.date.today())

    return Report(
        generated_at=today,
        lookback_days=lookback_days,
        grafana_url=grafana_url,
        prices=_price_moves(session, lookback_days),
        earnings=_upcoming_earnings(session, today),
        filings=_new_filings(session, today, lookback_days),
    )
