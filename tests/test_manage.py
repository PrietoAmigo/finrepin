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
from fintracker.models import Base, Instrument, Price, TickerRequest
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
