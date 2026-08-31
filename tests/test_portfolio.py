"""Unit tests for the portfolio ledger: the average-cost walk (which the
`portfolio_txn_state` view mirrors in SQL), transaction validation, and the
sector/region classifier. Runs against an in-memory SQLite DB, no network."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from fintracker import portfolio
from fintracker.ingest import classify
from fintracker.models import Base, Instrument, PortfolioTransaction, Price
from fintracker.portfolio import PortfolioError, Trade


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_price_id = count(1)


def _instrument(
    session: Session, symbol: str, *, kind: str = "equity", currency: str = "USD"
) -> Instrument:
    inst = Instrument(symbol=symbol, name=symbol, kind=kind, currency=currency)
    session.add(inst)
    session.flush()
    return inst


def _price(session: Session, inst: Instrument, day: dt.date, close: str) -> None:
    # SQLite does not autoincrement BIGINT primary keys, so ids are explicit.
    session.add(
        Price(
            id=next(_price_id),
            instrument_id=inst.id,
            date=day,
            close=Decimal(close),
            source="t",
        )
    )
    session.flush()


def _trade(side: str, quantity: str, price: str, fees: str = "0") -> Trade:
    return Trade(
        side=side, quantity=Decimal(quantity), price=Decimal(price), fees=Decimal(fees)
    )


class TestAverageCostWalk:
    def test_single_buy_sets_basis_including_fees(self) -> None:
        (state,) = portfolio.walk_transactions([_trade("buy", "10", "100", "5")])
        assert state.quantity == Decimal("10")
        assert state.cost_basis == Decimal("1005")
        assert state.avg_cost == Decimal("100.5")
        assert state.realized == Decimal("0")

    def test_second_buy_averages_the_cost(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("buy", "10", "200")]
        )
        assert states[-1].quantity == Decimal("20")
        assert states[-1].avg_cost == Decimal("150")

    def test_sell_realizes_against_the_average_and_leaves_it_alone(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("buy", "10", "200"), _trade("sell", "5", "250")]
        )
        final = states[-1]
        # 5 units sold at 250 against an average of 150 → 500 realized.
        assert final.realized == Decimal("500")
        assert final.quantity == Decimal("15")
        assert final.avg_cost == Decimal("150")
        assert final.cost_basis == Decimal("2250")

    def test_sell_fees_reduce_realized_pl(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("sell", "10", "120", "7")]
        )
        assert states[-1].realized == Decimal("193")
        assert states[-1].quantity == Decimal("0")
        assert states[-1].cost_basis == Decimal("0")

    def test_buying_back_after_a_full_sell_starts_a_fresh_average(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("sell", "10", "120"), _trade("buy", "5", "80")]
        )
        assert states[-1].avg_cost == Decimal("80")
        assert states[-1].realized == Decimal("200")

    def test_partial_sell_then_buy_averages_against_what_is_left(self) -> None:
        # The average is path-dependent: a sell removes units at the average,
        # so a later buy averages against the remainder, not everything bought.
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("sell", "6", "150"), _trade("buy", "6", "200")]
        )
        # 4 units left at 100 plus 6 at 200 → (400 + 1200) / 10.
        assert states[-1].avg_cost == Decimal("160")

    def test_over_sell_keeps_the_mistake_visible(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "10", "100"), _trade("sell", "12", "150")]
        )
        assert states[-1].quantity == Decimal("-2")
        assert states[-1].cost_basis == Decimal("0")
        # Proceeds on all 12, cost relieved only on the 10 actually held.
        assert states[-1].realized == Decimal("800")

    def test_selling_a_fractional_holding_whole_lands_on_exact_zero(self) -> None:
        # Decimal here and `numeric` in the portfolio_txn_state view both have to
        # land on a hard zero: a residue of 1e-17 would leave a closed position
        # showing up everywhere `quantity <> 0` is tested.
        states = portfolio.walk_transactions(
            [_trade("buy", "0.35", "61000", "12.50"), _trade("sell", "0.35", "90000")]
        )
        assert states[-1].quantity == Decimal("0")
        assert states[-1].cost_basis == Decimal("0")
        assert states[-1].realized == Decimal("10137.50")

    def test_state_is_returned_per_trade(self) -> None:
        states = portfolio.walk_transactions(
            [_trade("buy", "1", "10"), _trade("buy", "1", "20"), _trade("sell", "1", "30")]
        )
        assert [s.quantity for s in states] == [Decimal("1"), Decimal("2"), Decimal("1")]


class TestRecordTransaction:
    def _account(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR", broker="Interactive Brokers", currency="EUR")

    def test_records_a_buy_defaulting_to_the_listing_currency(self, session: Session) -> None:
        self._account(session)
        _instrument(session, "AI.PA", currency="EUR")
        txn = portfolio.record_transaction(
            session,
            account="IBKR",
            symbol="ai.pa",
            side="buy",
            quantity=Decimal("3"),
            price=Decimal("170"),
            trade_date=dt.date(2026, 3, 2),
        )
        assert txn.currency == "EUR"
        assert session.scalar(select(PortfolioTransaction)) is txn

    def test_explicit_trade_currency_wins(self, session: Session) -> None:
        self._account(session)
        _instrument(session, "CSU.TO", currency="CAD")
        txn = portfolio.record_transaction(
            session,
            account="IBKR",
            symbol="CSU.TO",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("3000"),
            trade_date=dt.date(2026, 3, 2),
            currency="usd",
        )
        assert txn.currency == "USD"

    def test_unknown_account_is_rejected(self, session: Session) -> None:
        _instrument(session, "AMZN")
        with pytest.raises(PortfolioError, match="unknown account"):
            portfolio.record_transaction(
                session,
                account="Nope",
                symbol="AMZN",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("1"),
                trade_date=dt.date(2026, 3, 2),
            )

    def test_untracked_symbol_is_rejected(self, session: Session) -> None:
        self._account(session)
        with pytest.raises(PortfolioError, match="not tracked"):
            portfolio.record_transaction(
                session,
                account="IBKR",
                symbol="ZZZZ",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("1"),
                trade_date=dt.date(2026, 3, 2),
            )

    def test_bad_side_is_rejected(self, session: Session) -> None:
        self._account(session)
        _instrument(session, "AMZN")
        with pytest.raises(PortfolioError, match="side must be"):
            portfolio.record_transaction(
                session,
                account="IBKR",
                symbol="AMZN",
                side="short",
                quantity=Decimal("1"),
                price=Decimal("1"),
                trade_date=dt.date(2026, 3, 2),
            )

    def test_adding_an_existing_account_updates_it(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR", currency="EUR")
        again = portfolio.add_account(session, "IBKR", broker="IB", currency="USD")
        assert again.broker == "IB"
        assert again.currency == "USD"
        assert len(session.scalars(select(portfolio.Account)).all()) == 1


class TestParsing:
    def test_missing_date_is_today(self) -> None:
        assert portfolio.parse_date(None) == dt.date.today()

    def test_iso_date_is_parsed(self) -> None:
        assert portfolio.parse_date("2026-01-31") == dt.date(2026, 1, 31)

    def test_garbage_date_is_rejected(self) -> None:
        with pytest.raises(PortfolioError, match="ISO date"):
            portfolio.parse_date("31/01/2026")

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(PortfolioError, match="greater than zero"):
            portfolio.parse_amount("0", "quantity")

    def test_fees_may_be_zero_but_not_negative(self) -> None:
        assert portfolio.parse_amount("0", "fees", positive=False) == Decimal("0")
        with pytest.raises(PortfolioError, match="cannot be negative"):
            portfolio.parse_amount("-1", "fees", positive=False)


class TestCurrentPositions:
    def test_position_is_derived_from_the_ledger(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR")
        inst = _instrument(session, "AMZN")
        _price(session, inst, dt.date(2026, 7, 1), "200")
        for side, qty, price in (("buy", "10", "100"), ("buy", "10", "200"), ("sell", "5", "250")):
            portfolio.record_transaction(
                session,
                account="IBKR",
                symbol="AMZN",
                side=side,
                quantity=Decimal(qty),
                price=Decimal(price),
                trade_date=dt.date(2026, 6, 1),
            )
        (position,) = portfolio.current_positions(session)
        assert position.quantity == Decimal("15")
        assert position.avg_cost == Decimal("150")
        assert position.last_price == Decimal("200")
        assert position.market_value == Decimal("3000")
        assert position.unrealized == Decimal("750")
        assert position.realized == Decimal("500")

    def test_the_same_symbol_in_two_accounts_stays_separate(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR")
        portfolio.add_account(session, "DEGIRO")
        _instrument(session, "AMZN")
        for account, qty in (("IBKR", "10"), ("DEGIRO", "4")):
            portfolio.record_transaction(
                session,
                account=account,
                symbol="AMZN",
                side="buy",
                quantity=Decimal(qty),
                price=Decimal("100"),
                trade_date=dt.date(2026, 6, 1),
            )
        positions = portfolio.current_positions(session)
        assert {(p.account, p.quantity) for p in positions} == {
            ("IBKR", Decimal("10")),
            ("DEGIRO", Decimal("4")),
        }

    def test_closed_positions_are_hidden_unless_asked_for(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR")
        _instrument(session, "AMZN")
        for side in ("buy", "sell"):
            portfolio.record_transaction(
                session,
                account="IBKR",
                symbol="AMZN",
                side=side,
                quantity=Decimal("5"),
                price=Decimal("100"),
                trade_date=dt.date(2026, 6, 1),
            )
        assert portfolio.current_positions(session) == []
        (closed,) = portfolio.current_positions(session, include_closed=True)
        assert closed.quantity == Decimal("0")

    def test_position_without_prices_reports_no_market_value(self, session: Session) -> None:
        portfolio.add_account(session, "IBKR")
        _instrument(session, "AMZN")
        portfolio.record_transaction(
            session,
            account="IBKR",
            symbol="AMZN",
            side="buy",
            quantity=Decimal("2"),
            price=Decimal("50"),
            trade_date=dt.date(2026, 6, 1),
        )
        (position,) = portfolio.current_positions(session)
        assert position.last_price is None
        assert position.market_value is None
        assert position.unrealized is None


class TestClassification:
    def test_equity_keeps_its_yahoo_sector_and_maps_the_country(self) -> None:
        assert classify.classify("equity", "Technology", "France") == ("Technology", "Europe")

    def test_unknown_country_falls_back_without_dropping_the_sector(self) -> None:
        assert classify.classify("equity", "Utilities", "Atlantis") == ("Utilities", "Other")

    def test_missing_sector_is_labelled_not_blank(self) -> None:
        sector, _ = classify.classify("equity", None, "United States")
        assert sector == classify.UNKNOWN_SECTOR

    def test_non_equities_use_their_kind_bucket_and_ignore_yahoo(self) -> None:
        assert classify.classify("crypto", "Technology", "United States") == ("Crypto", "Global")
        assert classify.classify("metal", None, None) == ("Precious metals", "Global")

    def test_unknown_kind_is_treated_like_an_equity(self) -> None:
        assert classify.buckets_for_kind("equity") is None
        assert classify.classify("equity", "Energy", "Canada") == ("Energy", "North America")
