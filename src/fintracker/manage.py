"""Manage the weekly-email watchlist, tickers, and portfolio holdings from the
command line.

This mirrors what the Grafana *Manage* dashboard does with its Business Forms
panels, but in plain Python so it is unit-testable and usable without Grafana:

    python -m fintracker.manage add-ticker NVDA CSU.TO
    python -m fintracker.manage watchlist show
    python -m fintracker.manage watchlist set AAPL BN UNH
    python -m fintracker.manage watchlist add BTC
    python -m fintracker.manage watchlist remove ETH
    python -m fintracker.manage account add "IBKR"
    python -m fintracker.manage account list
    python -m fintracker.manage holding add NVDA IBKR 10 1500.00 USD 2026-01-15
    python -m fintracker.manage holding reduce 3 4 750.00 2026-08-01
    python -m fintracker.manage holding list
    python -m fintracker.manage enrich

The watchlist is the ``instruments.in_watchlist`` flag the weekly report reads
(see ``report.data.build_report``); adding a ticker enqueues a ``ticker_requests``
row that the minutely scheduler job validates and ingests. Holdings are lots
(see ``fintracker.models.Holding``) backing the Grafana *Portfolio* dashboard's
value, allocation, and P/L panels (migration 0023's SQL views).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Account, Holding, Instrument, TickerRequest


def _clean(symbols: Iterable[str]) -> list[str]:
    """Uppercased, trimmed, de-duplicated symbols (order preserved)."""
    seen: dict[str, None] = {}
    for raw in symbols:
        symbol = raw.strip().upper()
        if symbol:
            seen.setdefault(symbol, None)
    return list(seen)


def _known_symbols(session: Session) -> set[str]:
    return set(session.execute(select(Instrument.symbol)).scalars().all())


def watchlist_symbols(session: Session) -> list[str]:
    """Symbols currently on the watchlist, sorted."""
    return sorted(
        session.execute(select(Instrument.symbol).where(Instrument.in_watchlist))
        .scalars()
        .all()
    )


def _split_known(session: Session, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
    wanted = _clean(symbols)
    known = _known_symbols(session)
    matched = [s for s in wanted if s in known]
    unknown = [s for s in wanted if s not in known]
    return matched, unknown


def set_watchlist(session: Session, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
    """Replace the whole watchlist with ``symbols``. Returns (applied, unknown)."""
    matched, unknown = _split_known(session, symbols)
    session.execute(update(Instrument).values(in_watchlist=False))
    if matched:
        session.execute(
            update(Instrument).where(Instrument.symbol.in_(matched)).values(in_watchlist=True)
        )
    return matched, unknown


def add_to_watchlist(session: Session, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
    """Flag ``symbols`` onto the watchlist, leaving the rest alone."""
    matched, unknown = _split_known(session, symbols)
    if matched:
        session.execute(
            update(Instrument).where(Instrument.symbol.in_(matched)).values(in_watchlist=True)
        )
    return matched, unknown


def remove_from_watchlist(
    session: Session, symbols: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Drop ``symbols`` from the watchlist, leaving the rest alone."""
    matched, unknown = _split_known(session, symbols)
    if matched:
        session.execute(
            update(Instrument).where(Instrument.symbol.in_(matched)).values(in_watchlist=False)
        )
    return matched, unknown


def enqueue_ticker(session: Session, symbol: str) -> tuple[str, str]:
    """Queue one symbol for ingestion. Returns (status, note).

    Mirrors the Add-ticker form: already-tracked symbols are a no-op, and a
    re-queued symbol is reset to ``pending`` so a previous failure can retry.
    """
    from fintracker.ingest.ondemand import normalize_symbol

    clean = normalize_symbol(symbol)
    if clean is None:
        return "not_found", "not a valid ticker symbol"
    if session.scalar(select(Instrument).where(Instrument.symbol == clean)) is not None:
        return "done", "already tracked"

    existing = session.scalar(select(TickerRequest).where(TickerRequest.symbol == clean))
    if existing is None:
        session.add(TickerRequest(symbol=clean))
    else:
        existing.status = "pending"
        existing.note = None
        existing.processed_at = None
    return "pending", "queued for ingestion"


def add_account(session: Session, name: str) -> Account:
    """Get-or-create an account by name (case-sensitive label, trimmed)."""
    clean = name.strip()
    account = session.scalar(select(Account).where(Account.name == clean))
    if account is None:
        account = Account(name=clean)
        session.add(account)
        session.flush()
    return account


def list_accounts(session: Session) -> list[str]:
    return sorted(session.scalars(select(Account.name)).all())


def add_holding(
    session: Session,
    symbol: str,
    account_name: str,
    quantity: Decimal,
    cost_basis: Decimal,
    currency: str,
    acquired_at: dt.date,
    note: str | None = None,
) -> tuple[str, str]:
    """Add one lot. Returns (status, note); status is 'added' or 'error'."""
    clean_symbol = symbol.strip().upper()
    instrument = session.scalar(select(Instrument).where(Instrument.symbol == clean_symbol))
    if instrument is None:
        return "error", f"{clean_symbol} is not a tracked instrument — add it as a ticker first"
    if quantity <= 0:
        return "error", "quantity must be positive"
    if cost_basis < 0:
        return "error", "cost basis cannot be negative"

    account = add_account(session, account_name)
    holding = Holding(
        instrument_id=instrument.id,
        account_id=account.id,
        quantity=quantity,
        cost_basis=cost_basis,
        currency=currency.strip().upper(),
        acquired_at=acquired_at,
        note=note,
    )
    session.add(holding)
    session.flush()
    return "added", f"holding #{holding.id}: {quantity} {clean_symbol} in {account.name}"


def reduce_holding(
    session: Session,
    holding_id: int,
    quantity: Decimal,
    proceeds: Decimal,
    sold_at: dt.date,
) -> tuple[str, str]:
    """Record a sale (partial or full) against an existing lot.

    Returns (status, note); status is 'reduced', 'closed', or 'error'.
    """
    holding = session.get(Holding, holding_id)
    if holding is None:
        return "error", f"no holding #{holding_id}"
    remaining = holding.quantity - holding.quantity_sold
    if quantity <= 0:
        return "error", "quantity must be positive"
    if quantity > remaining:
        return "error", f"only {remaining} left open on holding #{holding_id}"
    if proceeds < 0:
        return "error", "proceeds cannot be negative"

    holding.quantity_sold += quantity
    holding.proceeds += proceeds
    holding.last_sold_at = sold_at
    status = "closed" if holding.quantity_sold >= holding.quantity else "reduced"
    return status, f"holding #{holding_id}: sold {quantity}, {remaining - quantity} left open"


def list_holdings(
    session: Session, *, open_only: bool = True
) -> list[tuple[Holding, str, str]]:
    """Holdings with their instrument symbol and account name resolved."""
    stmt = (
        select(Holding, Instrument.symbol, Account.name)
        .join(Instrument, Instrument.id == Holding.instrument_id)
        .join(Account, Account.id == Holding.account_id)
        .order_by(Holding.acquired_at)
    )
    if open_only:
        stmt = stmt.where(Holding.quantity_sold < Holding.quantity)
    return [(h, symbol, account) for h, symbol, account in session.execute(stmt).all()]


def _print_watchlist(session: Session) -> None:
    symbols = watchlist_symbols(session)
    if symbols:
        print("Watchlist ({}): {}".format(len(symbols), ", ".join(symbols)))
    else:
        print("Watchlist is empty — the weekly email covers every tracked instrument.")


def _parse_decimal(raw: str, label: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        print(f"{label} must be a number, got {raw!r}", file=sys.stderr)
        return None


def _parse_date(raw: str, label: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        print(f"{label} must be YYYY-MM-DD, got {raw!r}", file=sys.stderr)
        return None


def _report(applied: list[str], unknown: list[str], verb: str) -> None:
    if applied:
        print(f"{verb}: {', '.join(applied)}")
    if unknown:
        print(f"Skipped (not tracked): {', '.join(unknown)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fintracker.manage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-ticker", help="queue one or more tickers for ingestion")
    add.add_argument("symbols", nargs="+")

    sub.add_parser(
        "enrich",
        help="backfill sector/region for equities that don't have them yet (Yahoo lookup)",
    )

    wl = sub.add_parser("watchlist", help="view or edit the weekly-email watchlist")
    wl_sub = wl.add_subparsers(dest="action", required=True)
    wl_sub.add_parser("show", help="print the current watchlist")
    for name in ("set", "add", "remove"):
        p = wl_sub.add_parser(name, help=f"{name} watchlist symbols")
        p.add_argument("symbols", nargs="+")

    acct = sub.add_parser("account", help="view or add portfolio accounts")
    acct_sub = acct.add_subparsers(dest="action", required=True)
    acct_sub.add_parser("list", help="print known accounts")
    acct_add = acct_sub.add_parser("add", help="create an account")
    acct_add.add_argument("name")

    hold = sub.add_parser("holding", help="add, reduce, or list portfolio holdings")
    hold_sub = hold.add_subparsers(dest="action", required=True)
    hold_add = hold_sub.add_parser("add", help="add a lot: quantity + cost basis + account")
    hold_add.add_argument("symbol")
    hold_add.add_argument("account")
    hold_add.add_argument("quantity")
    hold_add.add_argument("cost_basis")
    hold_add.add_argument("currency")
    hold_add.add_argument("acquired_at", help="YYYY-MM-DD")
    hold_add.add_argument("--note")
    hold_reduce = hold_sub.add_parser(
        "reduce", help="record a sale (partial or full) against a lot"
    )
    hold_reduce.add_argument("holding_id", type=int)
    hold_reduce.add_argument("quantity")
    hold_reduce.add_argument("proceeds")
    hold_reduce.add_argument("sold_at", help="YYYY-MM-DD")
    hold_list = hold_sub.add_parser("list", help="print holdings")
    hold_list.add_argument(
        "--all", dest="all_holdings", action="store_true", help="include fully closed lots"
    )

    args = parser.parse_args(argv)

    with session_scope() as session:
        if args.command == "add-ticker":
            for symbol in args.symbols:
                status, note = enqueue_ticker(session, symbol)
                print(f"{symbol.strip().upper()}: {status} ({note})")
        elif args.command == "enrich":
            from fintracker.ingest.ondemand import enrich_sector_region

            updated = enrich_sector_region(session)
            print(f"Updated sector/region for {updated} instrument(s).")
        elif args.command == "watchlist":
            if args.action == "show":
                _print_watchlist(session)
            elif args.action == "set":
                _report(*set_watchlist(session, args.symbols), verb="Watchlist set to")
            elif args.action == "add":
                _report(*add_to_watchlist(session, args.symbols), verb="Added")
            elif args.action == "remove":
                _report(*remove_from_watchlist(session, args.symbols), verb="Removed")
        elif args.command == "account":
            if args.action == "list":
                names = list_accounts(session)
                print(", ".join(names) if names else "No accounts yet.")
            elif args.action == "add":
                account = add_account(session, args.name)
                print(f"Account: {account.name}")
        elif args.command == "holding":
            if args.action == "add":
                quantity = _parse_decimal(args.quantity, "quantity")
                cost_basis = _parse_decimal(args.cost_basis, "cost_basis")
                acquired_at = _parse_date(args.acquired_at, "acquired_at")
                if quantity is None or cost_basis is None or acquired_at is None:
                    return 1
                status, note = add_holding(
                    session,
                    args.symbol,
                    args.account,
                    quantity,
                    cost_basis,
                    args.currency,
                    acquired_at,
                    note=args.note,
                )
                print(f"{status}: {note}")
            elif args.action == "reduce":
                quantity = _parse_decimal(args.quantity, "quantity")
                proceeds = _parse_decimal(args.proceeds, "proceeds")
                sold_at = _parse_date(args.sold_at, "sold_at")
                if quantity is None or proceeds is None or sold_at is None:
                    return 1
                status, note = reduce_holding(session, args.holding_id, quantity, proceeds, sold_at)
                print(f"{status}: {note}")
            elif args.action == "list":
                rows = list_holdings(session, open_only=not args.all_holdings)
                if not rows:
                    print("No holdings.")
                for h, symbol, account_name in rows:
                    open_qty = h.quantity - h.quantity_sold
                    print(
                        f"#{h.id} {symbol} · {account_name} · "
                        f"{open_qty}/{h.quantity} open · cost {h.cost_basis} {h.currency} "
                        f"· acquired {h.acquired_at}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
