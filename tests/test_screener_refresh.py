"""The Screener's materialized fundamentals follow every write to the facts.

`screener_fundamentals` (migration 0037) is refreshed after the SEC and
Yahoo fundamentals ingests, after an on-demand ticker is ingested, and at
boot (after the migrations, which may have changed the views it is built
from). A failed refresh is logged, and never fails the ingest that asked
for it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fintracker import run, screener
from fintracker.ingest import fundamentals, ondemand, yahoo_fundamentals
from fintracker.models import Base, Instrument, TickerRequest


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _use_session(monkeypatch: pytest.MonkeyPatch, module: Any, session: Session) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield session
        session.flush()

    monkeypatch.setattr(module, "session_scope", scope)


@pytest.fixture
def refreshes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for module in (fundamentals, yahoo_fundamentals, ondemand):
        monkeypatch.setattr(module, "refresh_screener", lambda m=module: calls.append(m.__name__))
    return calls


class _Engine:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.executed: list[str] = []

    @contextmanager
    def begin(self) -> Iterator[Any]:
        engine = self

        class Conn:
            def execute(self, statement: Any) -> None:
                if engine.fail:
                    raise RuntimeError("database went away")
                engine.executed.append(str(statement))

        yield Conn()


class TestRefreshScreener:
    def test_refreshes_concurrently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = _Engine()
        monkeypatch.setattr(screener, "get_engine", lambda: engine)
        assert screener.refresh_screener() is True
        assert engine.executed == [
            "REFRESH MATERIALIZED VIEW CONCURRENTLY screener_fundamentals"
        ]

    def test_a_failure_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(screener, "get_engine", lambda: _Engine(fail=True))
        with caplog.at_level(logging.ERROR, logger="fintracker.screener"):
            assert screener.refresh_screener() is False
        assert "Screener refresh failed" in caplog.text


def _equity(session: Session, symbol: str, **kw: Any) -> Instrument:
    inst = Instrument(symbol=symbol, name=symbol, kind="equity", currency="USD", **kw)
    session.add(inst)
    # Committed, as tracked instruments are: a failed company's rollback
    # mustn't take them with it.
    session.commit()
    return inst


class TestSecIngest:
    @pytest.fixture(autouse=True)
    def _stubs(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_session(monkeypatch, fundamentals, session)
        monkeypatch.setattr(fundamentals, "SecClient", lambda: object())
        monkeypatch.setattr(
            fundamentals, "get_settings", lambda: SimpleNamespace(sec_user_agent="me@example.com")
        )

    def test_refreshes_once_after_every_company(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, refreshes: list[str]
    ) -> None:
        _equity(session, "UNH", taxonomy="us-gaap", cik="0000731766")
        _equity(session, "AMZN", taxonomy="us-gaap", cik="0001018724")
        ingested: list[str] = []

        def ingest(s: Session, client: object, inst: Instrument) -> int:
            assert refreshes == []  # not before the facts are in
            if inst.symbol == "UNH":
                raise RuntimeError("SEC hiccup")
            ingested.append(inst.symbol)
            return 1

        monkeypatch.setattr(fundamentals, "ingest_instrument_facts", ingest)
        fundamentals.ingest_fundamentals()
        # One company failing doesn't stop the others' facts reaching the Screener.
        assert ingested == ["AMZN"]
        assert refreshes == ["fintracker.ingest.fundamentals"]

    def test_skipped_ingest_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, refreshes: list[str]
    ) -> None:
        monkeypatch.setattr(
            fundamentals, "get_settings", lambda: SimpleNamespace(sec_user_agent=None)
        )
        fundamentals.ingest_fundamentals()
        assert refreshes == []


def test_yahoo_ingest_refreshes_after_every_company(
    session: Session, monkeypatch: pytest.MonkeyPatch, refreshes: list[str]
) -> None:
    _use_session(monkeypatch, yahoo_fundamentals, session)
    _equity(session, "AI.PA", yahoo_symbol="AI.PA")
    _equity(session, "RMS.PA", yahoo_symbol="RMS.PA")
    ingested: list[str] = []

    def ingest(s: Session, inst: Instrument) -> int:
        assert refreshes == []
        ingested.append(inst.symbol)
        return 1

    monkeypatch.setattr(yahoo_fundamentals, "ingest_instrument_yahoo_facts", ingest)
    yahoo_fundamentals.ingest_yahoo_fundamentals()
    assert sorted(ingested) == ["AI.PA", "RMS.PA"]
    assert refreshes == ["fintracker.ingest.yahoo_fundamentals"]


class TestTickerRequests:
    @pytest.fixture(autouse=True)
    def _stubs(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_session(monkeypatch, ondemand, session)

    def _request(self, session: Session, symbol: str) -> None:
        session.add(TickerRequest(symbol=symbol, status="pending"))
        session.commit()

    def test_a_new_ticker_joins_the_screener(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, refreshes: list[str]
    ) -> None:
        self._request(session, "KNSL")
        self._request(session, "NOPE")
        results = {"KNSL": ("done", "5 SEC fundamentals facts"), "NOPE": ("not_found", "unknown")}
        monkeypatch.setattr(ondemand, "_resolve", lambda req, s: results[req.symbol])
        ondemand.process_ticker_requests()
        assert refreshes == ["fintracker.ingest.ondemand"]

    def test_nothing_ingested_nothing_refreshed(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, refreshes: list[str]
    ) -> None:
        ondemand.process_ticker_requests()  # no pending requests
        self._request(session, "NOPE")
        monkeypatch.setattr(ondemand, "_resolve", lambda req, s: ("not_found", "unknown"))
        ondemand.process_ticker_requests()
        assert refreshes == []


def test_boot_refreshes_after_the_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[str] = []
    settings = SimpleNamespace(log_level="INFO", tz="UTC", run_on_start=False)
    monkeypatch.setattr(run, "get_settings", lambda: settings)
    for name in ("wait_for_db", "run_migrations", "seed_instruments", "seed_housing",
                 "refresh_screener"):
        monkeypatch.setattr(run, name, lambda n=name: steps.append(n))
    monkeypatch.setattr(run.heartbeat, "beat", lambda: steps.append("beat"))
    monkeypatch.setattr(
        run, "build_scheduler", lambda: SimpleNamespace(start=lambda: steps.append("scheduler"))
    )
    run.main()
    assert steps.index("run_migrations") < steps.index("refresh_screener")
    assert steps.count("refresh_screener") == 1
    assert steps[-1] == "scheduler"
