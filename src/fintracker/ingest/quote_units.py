"""Yahoo quotes some listings in a currency's minor unit; store major units.

London prices come in pence (Yahoo currency ``GBp``), Johannesburg in cents
(``ZAc``) and Tel Aviv in agorot (``ILA``). Company statements, EPS and FX
rates are all in the major unit, so a price kept in cents made every
price-based ratio 100x too high. Worse, upper-casing the code turned ``ZAc``
into ``ZAC``, which no FX pair exists for, and ``GBp`` into ``GBP``, which
hides the pence entirely.

So prices are stored in the major unit: an instrument quoted in a minor unit
gets ``currency`` = GBP / ZAR / ILS and ``price_divisor`` = 100, and every
Yahoo bar is divided by it before it is stored (`scale_price_rows`).

`verify_quote_units` repairs instruments registered before this existed. It
asks Yahoo once for each equity whose divisor is still unknown (NULL). For a
minor-unit listing it divides the stored history by 100. It also moves the
trades recorded against it into the major unit: a trade price that matches
that day's close in minor units is divided by 100, one that already matches
the close in major units is only relabelled, and an inconclusive one follows
its currency label (``ZAC`` means cents, ``GBP`` means pounds). Every change is
logged.

    python -m fintracker.ingest.quote_units
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Instrument, PortfolioTransaction, Price

log = logging.getLogger(__name__)

# Yahoo's minor-unit currency codes -> (ISO currency, units per major unit).
# Case matters: "GBp" is pence while "GBP" is pounds. The upper-cased forms are
# what earlier versions stored; except for GBP they are not ISO codes, so they
# can only mean the minor unit.
MINOR_UNITS: dict[str, tuple[str, int]] = {
    "GBp": ("GBP", 100),
    "GBX": ("GBP", 100),
    "ZAc": ("ZAR", 100),
    "ZAC": ("ZAR", 100),
    "ILA": ("ILS", 100),
}

# Minor-unit codes that are not real currencies: never an FX pair, and a trade
# labelled with one is in the minor unit.
PSEUDO_CURRENCIES = frozenset({"GBX", "ZAC", "ILA"})

_PRICE_COLUMNS = ("open", "high", "low", "close")

# A trade price within this factor of a close counts as quoted in that unit.
_MATCH_FACTOR = 2.0
# How far from the trade date a close may be and still be compared.
_CLOSE_WINDOW = dt.timedelta(days=10)


def normalize_quote_currency(raw: str | None) -> tuple[str | None, int]:
    """(ISO currency, divisor) for a Yahoo currency code; (None, 1) when unknown."""
    if raw is None or not str(raw).strip():
        return None, 1
    code = str(raw).strip()
    if code in MINOR_UNITS:
        return MINOR_UNITS[code]
    upper = code.upper()
    if upper in PSEUDO_CURRENCIES:
        return MINOR_UNITS[upper]
    return upper, 1


def scale_price_rows(rows: list[dict[str, Any]], divisor: int | None) -> list[dict[str, Any]]:
    """Yahoo bars converted to the major unit; volume is left alone."""
    if not divisor or divisor == 1:
        return rows
    return [
        {
            **row,
            **{
                col: row[col] / divisor
                for col in _PRICE_COLUMNS
                if row.get(col) is not None
            },
        }
        for row in rows
    ]


def trade_in_minor_units(
    price: Decimal, close_minor: Decimal | None, divisor: int, *, label_is_minor: bool
) -> bool:
    """Whether a trade price was typed in the minor unit.

    `close_minor` is the stored close (still in the minor unit) nearest the
    trade date. A price near it was typed in the minor unit; a price near
    close / divisor was typed in the major unit; anything else (or no close)
    follows the trade's currency label.
    """
    if close_minor is None or close_minor <= 0 or price <= 0:
        return label_is_minor
    ratio = float(price / close_minor)
    if 1 / _MATCH_FACTOR <= ratio <= _MATCH_FACTOR:
        return True
    if 1 / _MATCH_FACTOR <= ratio * divisor <= _MATCH_FACTOR:
        return False
    return label_is_minor


@dataclass
class QuoteUnitFix:
    """What `verify_instrument` changed for one minor-unit instrument."""

    symbol: str
    old_currency: str
    currency: str
    divisor: int
    price_rows: int = 0
    trades: list[str] = field(default_factory=list)


def _plain(value: Decimal) -> str:
    """A Decimal without trailing zeros or exponent: 3050, 30.5."""
    return format(Decimal(value).normalize(), "f")


def _close_near(session: Session, instrument_id: int, day: dt.date) -> Decimal | None:
    """The stored close on or before `day` (else just after), within a short window."""
    before = session.scalar(
        select(Price.close)
        .where(
            Price.instrument_id == instrument_id,
            Price.date <= day,
            Price.date >= day - _CLOSE_WINDOW,
        )
        .order_by(Price.date.desc())
        .limit(1)
    )
    if before is not None:
        return Decimal(before)
    after = session.scalar(
        select(Price.close)
        .where(
            Price.instrument_id == instrument_id,
            Price.date > day,
            Price.date <= day + _CLOSE_WINDOW,
        )
        .order_by(Price.date)
        .limit(1)
    )
    return None if after is None else Decimal(after)


def _convert_trades(
    session: Session, inst: Instrument, old_currency: str, currency: str, divisor: int
) -> list[str]:
    """Move trades labelled in the listing's old currency into the major unit.

    Must run before the price history is rescaled: the comparison is against
    the stored closes in the minor unit.
    """
    labels = {old_currency} | PSEUDO_CURRENCIES
    trades = (
        session.execute(
            select(PortfolioTransaction)
            .where(
                PortfolioTransaction.instrument_id == inst.id,
                PortfolioTransaction.currency.in_(sorted(labels)),
            )
            .order_by(PortfolioTransaction.trade_date, PortfolioTransaction.id)
        )
        .scalars()
        .all()
    )
    notes = []
    for txn in trades:
        close = _close_near(session, inst.id, txn.trade_date)
        minor = trade_in_minor_units(
            Decimal(txn.price),
            close,
            divisor,
            label_is_minor=txn.currency in PSEUDO_CURRENCIES,
        )
        before = f"{_plain(txn.price)} {txn.currency}"
        if minor:
            txn.price = Decimal(txn.price) / divisor
            txn.fees = Decimal(txn.fees) / divisor
            how = f"price and fees divided by {divisor} (they were in the minor unit)"
        else:
            how = "relabelled only (already in the major unit)"
        txn.currency = currency
        notes.append(
            f"{txn.side} on {txn.trade_date}: {before} -> {_plain(txn.price)} {currency}, {how}"
        )
    return notes


def verify_instrument(session: Session, inst: Instrument, raw_currency: str) -> QuoteUnitFix | None:
    """Apply Yahoo's quote unit to one instrument; returns what changed, if anything.

    A major-unit listing only records its divisor, plus its currency when that
    was a guess: an instrument added while Yahoo didn't name its currency was
    stored as USD (see ingest.ondemand), but its prices are in Yahoo's currency.
    """
    currency, divisor = normalize_quote_currency(raw_currency)
    if currency is None:
        return None
    if divisor == 1:
        if inst.currency != currency:
            log.warning(
                "%s is quoted in %s, not %s: currency corrected",
                inst.symbol,
                currency,
                inst.currency,
            )
            inst.currency = currency
        inst.price_divisor = 1
        return None
    fix = QuoteUnitFix(
        symbol=inst.symbol, old_currency=inst.currency, currency=currency, divisor=divisor
    )
    fix.trades = _convert_trades(session, inst, inst.currency, currency, divisor)
    rescaled = session.execute(
        update(Price)
        .where(Price.instrument_id == inst.id)
        .values({col: getattr(Price, col) / divisor for col in _PRICE_COLUMNS})
        .execution_options(synchronize_session=False)
    )
    fix.price_rows = cast("CursorResult[Any]", rescaled).rowcount
    inst.currency = currency
    inst.price_divisor = divisor
    session.flush()
    return fix


def _yahoo_currency(yahoo_symbol: str) -> str | None:
    """Yahoo's quote currency, case preserved ("GBp" is not "GBP")."""
    import yfinance as yf

    currency = yf.Ticker(yahoo_symbol).fast_info["currency"]
    return str(currency) if currency else None


def verify_quote_units(fetch_currency: Callable[[str], str | None] = _yahoo_currency) -> int:
    """Check every unverified equity against Yahoo; returns how many were rescaled."""
    fixed = 0
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "equity",
                    Instrument.yahoo_symbol.is_not(None),
                    Instrument.price_divisor.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            assert inst.yahoo_symbol is not None
            try:
                raw = fetch_currency(inst.yahoo_symbol)
            except Exception:
                log.warning("Quote-unit check failed for %s; retrying next run", inst.symbol)
                continue
            if raw is None:
                continue
            fix = verify_instrument(session, inst, raw)
            if fix is None:
                continue
            fixed += 1
            log.warning(
                "%s is quoted in %s minor units: %d stored prices divided by %d, "
                "currency %s -> %s",
                fix.symbol,
                fix.currency,
                fix.price_rows,
                fix.divisor,
                fix.old_currency,
                fix.currency,
            )
            for note in fix.trades:
                log.warning("%s trade %s", fix.symbol, note)
    return fixed


if __name__ == "__main__":
    from fintracker.config import get_settings

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(f"Rescaled {verify_quote_units()} minor-unit instruments.")
