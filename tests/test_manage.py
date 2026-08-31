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

from fintracker import manage
from fintracker.models import Base, Holding, Instrument, Price, TickerRequest
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
        applied, unknown = manage.set_watchlist(session, ["aapl", "bn"])
        assert applied == ["AAPL", "BN"]
        assert unknown == []
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
        applied, unknown = manage.add_to_watchlist(session, ["AAPL", "ZZZZ"])
        assert applied == ["AAPL"]
        assert unknown == ["ZZZZ"]


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


class TestAccounts:
    def test_add_account_is_idempotent(self, session: Session) -> None:
        first = manage.add_account(session, "IBKR")
        second = manage.add_account(session, "IBKR")
        assert first.id == second.id
        assert manage.list_accounts(session) == ["IBKR"]

    def test_list_accounts_sorted(self, session: Session) -> None:
        manage.add_account(session, "Kraken")
        manage.add_account(session, "Coinbase")
        assert manage.list_accounts(session) == ["Coinbase", "Kraken"]


class TestHoldings:
    def test_add_holding_creates_lot_and_account(self, session: Session) -> None:
        _equity(session, "NVDA")
        status, note = manage.add_holding(
            session, "nvda", "IBKR", Decimal("10"), Decimal("1500.00"), "usd",
            dt.date(2026, 1, 15),
        )
        assert status == "added"
        assert "NVDA" in note
        holding = session.scalar(select(Holding))
        assert holding is not None
        assert holding.quantity == Decimal("10")
        assert holding.currency == "USD"
        assert manage.list_accounts(session) == ["IBKR"]

    def test_add_holding_rejects_unknown_symbol(self, session: Session) -> None:
        status, note = manage.add_holding(
            session, "ZZZZ", "IBKR", Decimal("10"), Decimal("100"), "USD", dt.date(2026, 1, 1)
        )
        assert status == "error"
        assert "not a tracked instrument" in note
        assert session.scalar(select(Holding)) is None

    def test_add_holding_rejects_bad_quantity(self, session: Session) -> None:
        _equity(session, "NVDA")
        status, _ = manage.add_holding(
            session, "NVDA", "IBKR", Decimal("0"), Decimal("100"), "USD", dt.date(2026, 1, 1)
        )
        assert status == "error"

    def test_reduce_holding_partial_then_full(self, session: Session) -> None:
        _equity(session, "NVDA")
        manage.add_holding(
            session, "NVDA", "IBKR", Decimal("10"), Decimal("1000"), "USD", dt.date(2026, 1, 1)
        )
        holding = session.scalar(select(Holding))
        assert holding is not None

        status, _ = manage.reduce_holding(
            session, holding.id, Decimal("4"), Decimal("500"), dt.date(2026, 6, 1)
        )
        assert status == "reduced"
        assert holding.quantity_sold == Decimal("4")
        assert holding.proceeds == Decimal("500")
        assert holding.last_sold_at == dt.date(2026, 6, 1)

        status, _ = manage.reduce_holding(
            session, holding.id, Decimal("6"), Decimal("900"), dt.date(2026, 7, 1)
        )
        assert status == "closed"
        assert holding.quantity_sold == Decimal("10")
        assert holding.proceeds == Decimal("1400")

    def test_reduce_holding_rejects_overselling(self, session: Session) -> None:
        _equity(session, "NVDA")
        manage.add_holding(
            session, "NVDA", "IBKR", Decimal("10"), Decimal("1000"), "USD", dt.date(2026, 1, 1)
        )
        holding = session.scalar(select(Holding))
        assert holding is not None
        status, note = manage.reduce_holding(
            session, holding.id, Decimal("11"), Decimal("100"), dt.date(2026, 6, 1)
        )
        assert status == "error"
        assert "only" in note
        assert holding.quantity_sold == Decimal("0")

    def test_reduce_holding_rejects_unknown_id(self, session: Session) -> None:
        status, note = manage.reduce_holding(
            session, 999, Decimal("1"), Decimal("1"), dt.date(2026, 1, 1)
        )
        assert status == "error"
        assert "no holding" in note

    def test_list_holdings_open_only_by_default(self, session: Session) -> None:
        _equity(session, "NVDA")
        manage.add_holding(
            session, "NVDA", "IBKR", Decimal("10"), Decimal("1000"), "USD", dt.date(2026, 1, 1)
        )
        holding = session.scalar(select(Holding))
        assert holding is not None
        manage.reduce_holding(
            session, holding.id, Decimal("10"), Decimal("1200"), dt.date(2026, 6, 1)
        )

        assert manage.list_holdings(session, open_only=True) == []
        rows = manage.list_holdings(session, open_only=False)
        assert len(rows) == 1
        _, symbol, account_name = rows[0]
        assert symbol == "NVDA"
        assert account_name == "IBKR"


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
        assert {row.symbol for row in report.prices} == {"AAPL", "MSFT"}

    def test_watchlist_filters_report(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=True)
        _equity(session, "MSFT")
        report = build_report(session)
        assert {row.symbol for row in report.prices} == {"AAPL"}

    def test_explicit_override_wins(self, session: Session) -> None:
        _equity(session, "AAPL", watchlist=True)
        _equity(session, "MSFT")
        report = build_report(session, symbols=["MSFT"])
        assert {row.symbol for row in report.prices} == {"MSFT"}
