"""Unit tests for the watchlist / add-ticker management helpers and the
report's watchlist precedence, exercised against an in-memory SQLite DB."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from fintracker import manage, portfolio
from fintracker.models import Account, Base, EarningsDate, Instrument, Price, TickerRequest
from fintracker.report.data import build_report


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_price_id = count(1)


def _equity(session: Session, symbol: str, *, watchlist: bool = False) -> Instrument:
    inst = Instrument(
        symbol=symbol, name=symbol, kind="equity", currency="USD", in_watchlist=watchlist
    )
    session.add(inst)
    session.flush()
    # Two bars so the report has a level to render. SQLite does not autoincrement
    # BIGINT primary keys, so the ids are assigned explicitly here.
    session.add_all(
        [
            Price(id=next(_price_id), instrument_id=inst.id, date=day, close=close, source="t")
            for day, close in (
                (dt.date(2026, 8, 19), Decimal("100")),
                (dt.date(2026, 8, 20), Decimal("110")),
            )
        ]
    )
    session.flush()
    return inst


class TestWatchlistEditing:
    def test_set_replaces_whole_list(self, session: Session) -> None:
        for sym in ("AAPL", "MSFT", "BN"):
            _equity(session, sym)
        applied, unknown, held = manage.set_watchlist(session, ["aapl", "bn"])
        assert applied == ["AAPL", "BN"]
        assert unknown == [] and held == []
        assert manage.watchlist_symbols(session) == ["AAPL", "BN"]
        # Setting again replaces rather than accumulates.
        manage.set_watchlist(session, ["MSFT"])
        assert manage.watchlist_symbols(session) == ["MSFT"]

    def test_add_and_remove_are_incremental(self, session: Session) -> None:
        for sym in ("AAPL", "MSFT", "BN"):
            _equity(session, sym)
        manage.add_to_watchlist(session, ["AAPL"])
        manage.add_to_watchlist(session, ["MSFT"])
        assert manage.watchlist_symbols(session) == ["AAPL", "MSFT"]
        manage.remove_from_watchlist(session, ["AAPL"])
        assert manage.watchlist_symbols(session) == ["MSFT"]

    def test_unknown_symbols_reported_not_applied(self, session: Session) -> None:
        _equity(session, "AAPL")
        applied, unknown, _ = manage.add_to_watchlist(session, ["AAPL", "ZZZZ"])
        assert applied == ["AAPL"]
        assert unknown == ["ZZZZ"]


def _trade(session: Session, symbol: str, side: str, quantity: str = "10") -> None:
    if session.scalar(select(Account)) is None:
        portfolio.add_account(session, "IBKR")
    portfolio.record_transaction(
        session,
        account="IBKR",
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal("100"),
        trade_date=dt.date(2026, 8, 20),
    )


class TestHoldingsStayOffTheWatchlist:
    def test_adding_a_holding_is_skipped(self, session: Session) -> None:
        for sym in ("AAPL", "MSFT"):
            _equity(session, sym)
        _trade(session, "AAPL", "buy")
        applied, unknown, held = manage.add_to_watchlist(session, ["AAPL", "MSFT"])
        assert applied == ["MSFT"]
        assert unknown == [] and held == ["AAPL"]
        assert manage.watchlist_symbols(session) == ["MSFT"]

    def test_set_skips_holdings_too(self, session: Session) -> None:
        for sym in ("AAPL", "MSFT"):
            _equity(session, sym)
        _trade(session, "AAPL", "buy")
        _, _, held = manage.set_watchlist(session, ["AAPL", "MSFT"])
        assert held == ["AAPL"]
        assert manage.watchlist_symbols(session) == ["MSFT"]

    def test_buying_a_watched_symbol_takes_it_off(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=True)
        _equity(session, "MSFT", watchlist=True)
        _trade(session, "AAPL", "buy")
        assert manage.watchlist_symbols(session) == ["MSFT"]

    def test_selling_out_makes_it_watchable_again(self, session: Session) -> None:
        _equity(session, "AAPL")
        _trade(session, "AAPL", "buy")
        _trade(session, "AAPL", "sell")
        applied, _, held = manage.add_to_watchlist(session, ["AAPL"])
        assert applied == ["AAPL"] and held == []

    def test_an_over_sell_elsewhere_does_not_hide_a_position(self, session: Session) -> None:
        _equity(session, "AAPL")
        _trade(session, "AAPL", "buy")
        portfolio.add_account(session, "DEGIRO")
        portfolio.record_transaction(
            session,
            account="DEGIRO",
            symbol="AAPL",
            side="sell",
            quantity=Decimal("10"),
            price=Decimal("100"),
            trade_date=dt.date(2026, 8, 20),
        )
        assert portfolio.held_symbols(session) == {"AAPL"}


class TestEnqueueTicker:
    def test_queues_new_symbol(self, session: Session) -> None:
        status, _ = manage.enqueue_ticker(session, "nvda")
        assert status == "pending"
        req = session.scalar(select(TickerRequest).where(TickerRequest.symbol == "NVDA"))
        assert req is not None and req.status == "pending"

    def test_already_tracked_is_noop(self, session: Session) -> None:
        _equity(session, "AAPL")
        status, note = manage.enqueue_ticker(session, "AAPL")
        assert status == "done" and note == "already tracked"
        assert session.scalar(select(TickerRequest)) is None

    def test_requeue_resets_failed_request(self, session: Session) -> None:
        session.add(TickerRequest(symbol="NVDA", status="not_found", note="was missing"))
        session.flush()
        status, _ = manage.enqueue_ticker(session, "NVDA")
        assert status == "pending"
        req = session.scalar(select(TickerRequest).where(TickerRequest.symbol == "NVDA"))
        assert req is not None and req.status == "pending" and req.note is None

    def test_rejects_garbage(self, session: Session) -> None:
        status, _ = manage.enqueue_ticker(session, "'; DROP TABLE instruments; --")
        assert status == "not_found"
        assert session.scalar(select(TickerRequest)) is None


class TestReportWatchlistPrecedence:
    @pytest.fixture(autouse=True)
    def _no_mvrv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The MVRV Z-Score sub-query is Postgres-only (::float8, stddev_pop) and
        # is irrelevant to watchlist selection, so stub it out for SQLite.
        monkeypatch.setattr("fintracker.report.data._mvrv_zscore_row", lambda *a, **k: None)

    def test_empty_watchlist_covers_all(self, session: Session) -> None:
        _equity(session, "AAPL")
        _equity(session, "MSFT")
        report = build_report(session)
        assert {row.symbol for row in report.watchlist} == {"AAPL", "MSFT"}
        assert report.watchlist_is_fallback

    def test_watchlist_filters_report(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=True)
        _equity(session, "MSFT")
        report = build_report(session)
        assert {row.symbol for row in report.watchlist} == {"AAPL"}
        assert not report.watchlist_is_fallback

    def test_explicit_override_wins(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=True)
        _equity(session, "MSFT")
        report = build_report(session, symbols=["MSFT"])
        assert {row.symbol for row in report.watchlist} == {"MSFT"}


class TestReportHoldings:
    @pytest.fixture(autouse=True)
    def _no_mvrv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fintracker.report.data._mvrv_zscore_row", lambda *a, **k: None)

    def test_holdings_are_always_listed_apart_from_the_watchlist(
        self, session: Session
    ) -> None:
        _equity(session, "AAPL")
        _equity(session, "MSFT", watchlist=True)
        _equity(session, "BN")
        _trade(session, "AAPL", "buy")
        report = build_report(session)
        assert [row.symbol for row in report.holdings] == ["AAPL"]
        assert [row.symbol for row in report.watchlist] == ["MSFT"]

    def test_a_holding_is_never_listed_twice(self, session: Session) -> None:
        _equity(session, "AAPL")
        _equity(session, "MSFT")
        _trade(session, "AAPL", "buy")
        # Neither the empty-watchlist fallback nor an explicit list repeats it.
        assert {r.symbol for r in build_report(session).watchlist} == {"MSFT"}
        override = build_report(session, symbols=["AAPL", "MSFT"])
        assert {r.symbol for r in override.watchlist} == {"MSFT"}

    def test_sold_out_positions_leave_holdings(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=False)
        _trade(session, "AAPL", "buy")
        _trade(session, "AAPL", "sell")
        assert build_report(session).holdings == []

    def test_held_eth_is_shown_although_the_fallback_skips_it(self, session: Session) -> None:
        _equity(session, "ETH")
        _equity(session, "AAPL")
        assert {r.symbol for r in build_report(session).watchlist} == {"AAPL"}
        _trade(session, "ETH", "buy")
        assert [r.symbol for r in build_report(session).holdings] == ["ETH"]

    def test_earnings_cover_both_and_say_which_is_which(self, session: Session) -> None:
        aapl = _equity(session, "AAPL")
        msft = _equity(session, "MSFT", watchlist=True)
        other = _equity(session, "BN")
        _trade(session, "AAPL", "buy")
        today = dt.date(2026, 11, 3)
        for inst, days in ((aapl, 10), (msft, 2), (other, 5)):
            session.add(
                EarningsDate(
                    instrument_id=inst.id,
                    earnings_date=today + dt.timedelta(days=days),
                    is_estimated=False,
                )
            )
        session.flush()
        report = build_report(session, today=today)
        assert [(e.symbol, e.held) for e in report.earnings] == [
            ("MSFT", False),
            ("AAPL", True),
        ]
