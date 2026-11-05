"""Minor-unit quotes (pence, cents, agorot): normalization, row scaling, and the
one-time repair of instruments and trades stored before it, on in-memory SQLite."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from fintracker.ingest import quote_units
from fintracker.ingest.forex import fx_instrument_rows
from fintracker.ingest.quote_units import (
    normalize_quote_currency,
    scale_price_rows,
    trade_in_minor_units,
    verify_instrument,
    verify_quote_units,
)
from fintracker.models import Account, Base, Instrument, PortfolioTransaction, Price

DAY = dt.date(2026, 10, 1)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_ids = count(1)


def _listing(session: Session, symbol: str, currency: str, close: str) -> Instrument:
    """An equity as earlier versions stored it: quotes as Yahoo sent them."""
    inst = Instrument(
        symbol=symbol, name=symbol, kind="equity", currency=currency, yahoo_symbol=symbol
    )
    session.add(inst)
    session.flush()
    for offset in range(3):
        session.add(
            Price(
                id=next(_ids),  # SQLite does not autoincrement BIGINT keys
                instrument_id=inst.id,
                date=DAY + dt.timedelta(days=offset),
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=1000,
                source="yfinance",
            )
        )
    session.flush()
    return inst


def _trade(
    session: Session, inst: Instrument, price: str, currency: str, *, fees: str = "0",
    day: dt.date = DAY,
) -> PortfolioTransaction:
    account = session.scalar(select(Account))
    if account is None:
        account = Account(name="IBKR", currency="EUR")
        session.add(account)
        session.flush()
    txn = PortfolioTransaction(
        account_id=account.id,
        instrument_id=inst.id,
        trade_date=day,
        side="buy",
        quantity=Decimal("10"),
        price=Decimal(price),
        fees=Decimal(fees),
        currency=currency,
    )
    session.add(txn)
    session.flush()
    return txn


def _closes(session: Session, inst: Instrument) -> list[Decimal]:
    return [
        Decimal(c)
        for c in session.scalars(select(Price.close).where(Price.instrument_id == inst.id))
    ]


class TestNormalizeQuoteCurrency:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("GBp", ("GBP", 100)),
            ("GBX", ("GBP", 100)),
            ("ZAc", ("ZAR", 100)),
            ("ZAC", ("ZAR", 100)),  # what earlier versions stored for "ZAc"
            ("ILA", ("ILS", 100)),
            ("GBP", ("GBP", 1)),  # pounds, not pence: case matters
            ("eur", ("EUR", 1)),
            (" CAD ", ("CAD", 1)),
            (None, (None, 1)),
            ("", (None, 1)),
        ],
    )
    def test_codes(self, raw: str | None, expected: tuple[str | None, int]) -> None:
        assert normalize_quote_currency(raw) == expected


class TestScalePriceRows:
    def test_divides_ohlc_and_keeps_volume(self) -> None:
        rows = [{"date": DAY, "open": 3000.0, "high": 3100.0, "low": None, "close": 3050.0,
                 "volume": 7}]
        (row,) = scale_price_rows(rows, 100)
        assert (row["open"], row["high"], row["low"], row["close"]) == (30.0, 31.0, None, 30.5)
        assert row["volume"] == 7 and row["date"] == DAY

    @pytest.mark.parametrize("divisor", [1, None])
    def test_major_units_pass_through(self, divisor: int | None) -> None:
        rows = [{"date": DAY, "close": 12.5}]
        assert scale_price_rows(rows, divisor) is rows


class TestTradeInMinorUnits:
    def test_price_matching_the_minor_unit_close(self) -> None:
        assert trade_in_minor_units(Decimal("3045"), Decimal("3050"), 100, label_is_minor=False)

    def test_price_matching_the_major_unit_close(self) -> None:
        assert not trade_in_minor_units(Decimal("30.4"), Decimal("3050"), 100, label_is_minor=True)

    def test_no_close_follows_the_label(self) -> None:
        assert trade_in_minor_units(Decimal("30.4"), None, 100, label_is_minor=True)
        assert not trade_in_minor_units(Decimal("3045"), None, 100, label_is_minor=False)

    def test_far_from_both_follows_the_label(self) -> None:
        assert not trade_in_minor_units(Decimal("500"), Decimal("3050"), 100, label_is_minor=False)


class TestVerifyInstrument:
    def test_cents_listing_is_rescaled_and_relabelled(self, session: Session) -> None:
        inst = _listing(session, "ART.JO", "ZAC", "3050")
        fix = verify_instrument(session, inst, "ZAc")
        assert fix is not None and fix.price_rows == 3
        assert (inst.currency, inst.price_divisor) == ("ZAR", 100)
        assert _closes(session, inst) == [Decimal("30.5")] * 3

    def test_trades_follow_the_close_or_their_label(self, session: Session) -> None:
        inst = _listing(session, "ART.JO", "ZAC", "3050")
        in_cents = _trade(session, inst, "3045", "ZAC", fees="1500")
        in_rand = _trade(session, inst, "30.40", "ZAC", fees="15")
        no_close = _trade(session, inst, "2500", "ZAC", day=DAY - dt.timedelta(days=400))
        in_euros = _trade(session, inst, "1.55", "EUR")
        fix = verify_instrument(session, inst, "ZAc")
        assert fix is not None and len(fix.trades) == 3
        assert (in_cents.price, in_cents.fees, in_cents.currency) == (
            Decimal("30.45"), Decimal("15"), "ZAR")
        assert (in_rand.price, in_rand.fees, in_rand.currency) == (
            Decimal("30.40"), Decimal("15"), "ZAR")
        # No close to compare with: "ZAC" says cents.
        assert (no_close.price, no_close.currency) == (Decimal("25"), "ZAR")
        # A trade in another currency is left alone.
        assert (in_euros.price, in_euros.currency) == (Decimal("1.55"), "EUR")

    def test_pence_stored_as_gbp(self, session: Session) -> None:
        # "GBp" was upper-cased to "GBP", so only Yahoo can tell it's pence.
        inst = _listing(session, "SHEL.L", "GBP", "2700")
        in_pence = _trade(session, inst, "2690", "GBP")
        in_pounds = _trade(session, inst, "26.95", "GBP")
        no_close = _trade(session, inst, "27.10", "GBP", day=DAY - dt.timedelta(days=400))
        verify_instrument(session, inst, "GBp")
        assert (inst.currency, inst.price_divisor) == ("GBP", 100)
        assert _closes(session, inst) == [Decimal("27")] * 3
        assert in_pence.price == Decimal("26.90")
        assert in_pounds.price == Decimal("26.95")
        # No close to compare with: "GBP" says pounds.
        assert no_close.price == Decimal("27.10")

    def test_major_unit_listing_only_records_the_divisor(self, session: Session) -> None:
        inst = _listing(session, "CSU.TO", "CAD", "6800")
        trade = _trade(session, inst, "6790", "CAD")
        assert verify_instrument(session, inst, "CAD") is None
        assert (inst.currency, inst.price_divisor) == ("CAD", 1)
        assert _closes(session, inst) == [Decimal("6800")] * 3
        assert trade.price == Decimal("6790")

    def test_guessed_currency_is_corrected(self, session: Session) -> None:
        # ingest.ondemand stores USD when Yahoo didn't name the currency at
        # registration; the prices it stored are in Yahoo's currency all along.
        inst = _listing(session, "AI.PA", "USD", "170")
        assert verify_instrument(session, inst, "EUR") is None
        assert (inst.currency, inst.price_divisor) == ("EUR", 1)
        assert _closes(session, inst) == [Decimal("170")] * 3


class TestVerifyQuoteUnits:
    @pytest.fixture(autouse=True)
    def _use_session(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        @contextmanager
        def scope() -> Iterator[Session]:
            yield session
            session.flush()

        monkeypatch.setattr(quote_units, "session_scope", scope)

    def test_asks_yahoo_once_per_equity(self, session: Session) -> None:
        art = _listing(session, "ART.JO", "ZAC", "3050")
        _listing(session, "CSU.TO", "CAD", "6800")
        asked: list[str] = []

        def yahoo(symbol: str) -> str:
            asked.append(symbol)
            return {"ART.JO": "ZAc", "CSU.TO": "CAD"}[symbol]

        assert verify_quote_units(yahoo) == 1
        assert sorted(asked) == ["ART.JO", "CSU.TO"]
        # Both are verified now: a second run asks Yahoo nothing and rescales nothing.
        assert verify_quote_units(yahoo) == 0
        assert len(asked) == 2
        assert _closes(session, art) == [Decimal("30.5")] * 3

    def test_unanswered_check_is_retried(self, session: Session) -> None:
        inst = _listing(session, "ART.JO", "ZAC", "3050")

        def down(symbol: str) -> str:
            raise ConnectionError("Yahoo unreachable")

        assert verify_quote_units(down) == 0
        assert inst.price_divisor is None
        assert _closes(session, inst) == [Decimal("3050")] * 3
        assert verify_quote_units(lambda s: "ZAc") == 1


class TestFxPairs:
    def test_no_pair_for_minor_unit_codes(self) -> None:
        rows = fx_instrument_rows({"ZAC", "GBX", "ILA", "ZAR"})
        assert [r["symbol"] for r in rows] == ["ZAR/USD"]
