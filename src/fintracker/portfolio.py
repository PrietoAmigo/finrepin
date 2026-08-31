"""Portfolio ledger: accounts, buys and sells, and the positions they imply.

The database stores *transactions*, not holdings — quantity, cost basis, and
realized P/L are derived from the ledger, so correcting a mistyped trade fixes
every number at once. The Grafana *Portfolio* dashboard derives them in SQL
(the ``portfolio_*`` views from migration 0022); ``walk_transactions`` below is
the same walk in Python, which keeps this CLI usable without Grafana and gives
the arithmetic a home the unit tests can reach.

Both use the **average-cost** method: a buy raises the average cost per unit
(fees included), a sell books ``proceeds − fees − units × average cost`` as
realized P/L and leaves the average untouched. (Spanish IRPF uses FIFO for
capital gains; this is a portfolio tracker, not a tax return.)

    python -m fintracker.portfolio account add "IBKR" --broker "Interactive Brokers"
    python -m fintracker.portfolio account list
    python -m fintracker.portfolio buy IBKR AMZN 12 --price 178.40 --date 2026-02-03 --fees 1.20
    python -m fintracker.portfolio sell IBKR AMZN 4 --price 214.10 --date 2026-07-19
    python -m fintracker.portfolio positions
    python -m fintracker.portfolio transactions --limit 20

Amounts are quoted in the trade's own currency (``--currency``, defaulting to
the instrument's listing currency); the dashboard converts everything through
``fx_usd_daily``. The CLI's ``positions`` view reports in each instrument's own
currency and so does not convert — it is a quick check, not the dashboard.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Account, Instrument, PortfolioTransaction, Price

ZERO = Decimal("0")


class PortfolioError(Exception):
    """A transaction or account could not be recorded as asked."""


@dataclass(frozen=True)
class Trade:
    """One ledger entry, currency-agnostic — the input to the average-cost walk."""

    side: str  # buy | sell
    quantity: Decimal
    price: Decimal
    fees: Decimal = ZERO


@dataclass(frozen=True)
class PositionState:
    """Running state after applying a trade: what is held and what was booked."""

    quantity: Decimal
    cost_basis: Decimal
    realized: Decimal

    @property
    def avg_cost(self) -> Decimal:
        return self.cost_basis / self.quantity if self.quantity else ZERO


def walk_transactions(trades: Iterable[Trade]) -> list[PositionState]:
    """Apply ``trades`` in order, returning the state after each one.

    Mirrors the ``portfolio_txn_state`` view. An over-sell (selling more units
    than are held — a data-entry slip) realises the full proceeds, drops the
    basis to zero, and leaves the quantity negative so the mistake is visible
    rather than silently absorbed.
    """
    quantity = cost = realized = ZERO
    states: list[PositionState] = []
    for trade in trades:
        if trade.side == "buy":
            quantity += trade.quantity
            cost += trade.quantity * trade.price + trade.fees
        else:
            avg = cost / quantity if quantity > 0 else ZERO
            sold = min(trade.quantity, max(quantity, ZERO))
            realized += trade.quantity * trade.price - trade.fees - sold * avg
            quantity -= trade.quantity
            cost = max(quantity, ZERO) * avg
        states.append(PositionState(quantity=quantity, cost_basis=cost, realized=realized))
    return states


def parse_date(raw: str | None) -> dt.date:
    """An ISO date, or today when omitted."""
    if not raw:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise PortfolioError(f"{raw!r} is not an ISO date (YYYY-MM-DD)") from exc


def parse_amount(raw: str, field: str, *, positive: bool = True) -> Decimal:
    try:
        value = Decimal(str(raw))
    except Exception as exc:
        raise PortfolioError(f"{field} {raw!r} is not a number") from exc
    if positive and value <= 0:
        raise PortfolioError(f"{field} must be greater than zero (got {value})")
    if not positive and value < 0:
        raise PortfolioError(f"{field} cannot be negative (got {value})")
    return value


def add_account(
    session: Session,
    name: str,
    *,
    broker: str | None = None,
    currency: str = "EUR",
    note: str | None = None,
) -> Account:
    """Register an account, or update the details of one that already exists."""
    clean = name.strip()
    if not clean:
        raise PortfolioError("account name cannot be empty")
    account = session.scalar(select(Account).where(Account.name == clean))
    if account is None:
        account = Account(name=clean, broker=broker, currency=currency.strip().upper(), note=note)
        session.add(account)
        session.flush()
        return account
    if broker is not None:
        account.broker = broker
    if note is not None:
        account.note = note
    account.currency = currency.strip().upper()
    return account


def _require_account(session: Session, name: str) -> Account:
    account = session.scalar(select(Account).where(Account.name == name.strip()))
    if account is None:
        known = sorted(session.scalars(select(Account.name)))
        hint = ", ".join(known) if known else "none yet — add one first"
        raise PortfolioError(f"unknown account {name!r} (known: {hint})")
    return account


def _require_instrument(session: Session, symbol: str) -> Instrument:
    clean = symbol.strip().upper()
    instrument = session.scalar(select(Instrument).where(Instrument.symbol == clean))
    if instrument is None:
        raise PortfolioError(
            f"{clean} is not tracked — add it first "
            f"(python -m fintracker.manage add-ticker {clean})"
        )
    return instrument


def record_transaction(
    session: Session,
    *,
    account: str,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    trade_date: dt.date,
    fees: Decimal = ZERO,
    currency: str | None = None,
    note: str | None = None,
) -> PortfolioTransaction:
    """Append one buy or sell to the ledger.

    The instrument must already be tracked (its price history is what makes the
    position worth anything); the trade currency defaults to the instrument's
    listing currency.
    """
    if side not in ("buy", "sell"):
        raise PortfolioError(f"side must be 'buy' or 'sell', not {side!r}")
    acct = _require_account(session, account)
    instrument = _require_instrument(session, symbol)
    txn = PortfolioTransaction(
        account_id=acct.id,
        instrument_id=instrument.id,
        trade_date=trade_date,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        currency=(currency or instrument.currency).strip().upper(),
        note=note,
    )
    session.add(txn)
    session.flush()
    return txn


@dataclass(frozen=True)
class PositionRow:
    """One current position, reported in the instrument's own currency."""

    account: str
    symbol: str
    currency: str
    quantity: Decimal
    cost_basis: Decimal
    avg_cost: Decimal
    last_price: Decimal | None
    realized: Decimal

    @property
    def market_value(self) -> Decimal | None:
        return None if self.last_price is None else self.quantity * self.last_price

    @property
    def unrealized(self) -> Decimal | None:
        value = self.market_value
        return None if value is None else value - self.cost_basis


def current_positions(session: Session, *, include_closed: bool = False) -> list[PositionRow]:
    """Walk the whole ledger and return the position each (account, symbol) holds."""
    rows = session.execute(
        select(PortfolioTransaction, Account.name, Instrument.symbol, Instrument.currency)
        .join(Account, Account.id == PortfolioTransaction.account_id)
        .join(Instrument, Instrument.id == PortfolioTransaction.instrument_id)
        .order_by(PortfolioTransaction.trade_date, PortfolioTransaction.id)
    ).all()

    ledgers: dict[tuple[str, str, str], list[Trade]] = {}
    for txn, account, symbol, currency in rows:
        key = (account, symbol, currency)
        ledgers.setdefault(key, []).append(
            Trade(side=txn.side, quantity=txn.quantity, price=txn.price, fees=txn.fees)
        )

    positions: list[PositionRow] = []
    for (account, symbol, currency), trades in sorted(ledgers.items()):
        state = walk_transactions(trades)[-1]
        if state.quantity == ZERO and not include_closed:
            continue
        positions.append(
            PositionRow(
                account=account,
                symbol=symbol,
                currency=currency,
                quantity=state.quantity,
                cost_basis=state.cost_basis,
                avg_cost=state.avg_cost,
                last_price=_last_close(session, symbol),
                realized=state.realized,
            )
        )
    return positions


def _last_close(session: Session, symbol: str) -> Decimal | None:
    return session.scalar(
        select(Price.close)
        .join(Instrument, Instrument.id == Price.instrument_id)
        .where(Instrument.symbol == symbol)
        .order_by(Price.date.desc())
        .limit(1)
    )


def _fmt(value: Decimal | None, places: int = 2) -> str:
    return "—" if value is None else f"{value:,.{places}f}"


def _print_positions(session: Session, *, include_closed: bool) -> None:
    positions = current_positions(session, include_closed=include_closed)
    if not positions:
        print("No positions yet — record a buy first.")
        return
    header = f"{'Account':<14} {'Symbol':<10} {'Qty':>14} {'Avg cost':>12} "
    header += f"{'Last':>12} {'Cost':>14} {'Value':>14} {'Unreal.':>14} {'Realized':>12}  Ccy"
    print(header)
    for p in positions:
        print(
            f"{p.account:<14.14} {p.symbol:<10.10} {_fmt(p.quantity, 8):>14} "
            f"{_fmt(p.avg_cost, 4):>12} {_fmt(p.last_price, 4):>12} "
            f"{_fmt(p.cost_basis):>14} {_fmt(p.market_value):>14} "
            f"{_fmt(p.unrealized):>14} {_fmt(p.realized):>12}  {p.currency}"
        )
    print(
        "\nValues are in each instrument's own currency and are not summed; "
        "the Grafana Portfolio dashboard converts and totals them."
    )


def _print_transactions(session: Session, limit: int) -> None:
    rows = session.execute(
        select(PortfolioTransaction, Account.name, Instrument.symbol)
        .join(Account, Account.id == PortfolioTransaction.account_id)
        .join(Instrument, Instrument.id == PortfolioTransaction.instrument_id)
        .order_by(PortfolioTransaction.trade_date.desc(), PortfolioTransaction.id.desc())
        .limit(limit)
    ).all()
    if not rows:
        print("The ledger is empty.")
        return
    print(f"{'Date':<12} {'Account':<14} {'Symbol':<10} {'Side':<5} {'Qty':>14} {'Price':>12}  Ccy")
    for txn, account, symbol in rows:
        print(
            f"{txn.trade_date.isoformat():<12} {account:<14.14} {symbol:<10.10} "
            f"{txn.side:<5} {_fmt(txn.quantity, 8):>14} "
            f"{_fmt(txn.price, 4):>12}  {txn.currency}"
        )


def _print_accounts(session: Session) -> None:
    accounts = session.scalars(select(Account).order_by(Account.name)).all()
    if not accounts:
        print("No accounts yet — python -m fintracker.portfolio account add <name>")
        return
    for a in accounts:
        broker = f" · {a.broker}" if a.broker else ""
        note = f" · {a.note}" if a.note else ""
        print(f"{a.name} ({a.currency}){broker}{note}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fintracker.portfolio", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    account = sub.add_parser("account", help="add or list accounts")
    account_sub = account.add_subparsers(dest="action", required=True)
    account_add = account_sub.add_parser("add", help="register (or update) an account")
    account_add.add_argument("name")
    account_add.add_argument("--broker")
    account_add.add_argument("--currency", default="EUR", help="base currency (default EUR)")
    account_add.add_argument("--note")
    account_sub.add_parser("list", help="list accounts")

    for side in ("buy", "sell"):
        trade = sub.add_parser(side, help=f"record a {side}")
        trade.add_argument("account")
        trade.add_argument("symbol")
        trade.add_argument("quantity")
        trade.add_argument("--price", required=True, help="price per unit, in --currency")
        trade.add_argument("--date", help="trade date (ISO, default today)")
        trade.add_argument("--fees", default="0", help="commissions and taxes (default 0)")
        trade.add_argument("--currency", help="trade currency (default: listing currency)")
        trade.add_argument("--note")

    positions = sub.add_parser("positions", help="show current positions")
    positions.add_argument(
        "--include-closed", action="store_true", help="also show fully sold positions"
    )

    txns = sub.add_parser("transactions", help="show the most recent ledger entries")
    txns.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with session_scope() as session:
            if args.command == "account":
                if args.action == "add":
                    account = add_account(
                        session,
                        args.name,
                        broker=args.broker,
                        currency=args.currency,
                        note=args.note,
                    )
                    print(f"Account {account.name} ({account.currency}) ready.")
                else:
                    _print_accounts(session)
            elif args.command in ("buy", "sell"):
                txn = record_transaction(
                    session,
                    account=args.account,
                    symbol=args.symbol,
                    side=args.command,
                    quantity=parse_amount(args.quantity, "quantity"),
                    price=parse_amount(args.price, "price", positive=False),
                    trade_date=parse_date(args.date),
                    fees=parse_amount(args.fees, "fees", positive=False),
                    currency=args.currency,
                    note=args.note,
                )
                print(
                    f"Recorded {txn.side} {txn.quantity} {args.symbol.upper()} "
                    f"@ {txn.price} {txn.currency} on {txn.trade_date} ({args.account})."
                )
            elif args.command == "positions":
                _print_positions(session, include_closed=args.include_closed)
            elif args.command == "transactions":
                _print_transactions(session, args.limit)
    except PortfolioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
