"""Manage the weekly-email watchlist and queue tickers from the command line.

This mirrors what the Grafana *Manage* dashboard does with its Business Forms
panels, but in plain Python so it is unit-testable and usable without Grafana:

    python -m fintracker.manage add-ticker NVDA CSU.TO
    python -m fintracker.manage watchlist show
    python -m fintracker.manage watchlist set AAPL BN UNH
    python -m fintracker.manage watchlist add BTC
    python -m fintracker.manage watchlist remove ETH

The watchlist is the ``instruments.in_watchlist`` flag the weekly report reads
(see ``report.data.build_report``); adding a ticker enqueues a ``ticker_requests``
row that the minutely scheduler job validates and ingests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.models import Instrument, TickerRequest


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


def _print_watchlist(session: Session) -> None:
    symbols = watchlist_symbols(session)
    if symbols:
        print("Watchlist ({}): {}".format(len(symbols), ", ".join(symbols)))
    else:
        print("Watchlist is empty — the weekly email covers every tracked instrument.")


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

    wl = sub.add_parser("watchlist", help="view or edit the weekly-email watchlist")
    wl_sub = wl.add_subparsers(dest="action", required=True)
    wl_sub.add_parser("show", help="print the current watchlist")
    for name in ("set", "add", "remove"):
        p = wl_sub.add_parser(name, help=f"{name} watchlist symbols")
        p.add_argument("symbols", nargs="+")

    args = parser.parse_args(argv)

    with session_scope() as session:
        if args.command == "add-ticker":
            for symbol in args.symbols:
                status, note = enqueue_ticker(session, symbol)
                print(f"{symbol.strip().upper()}: {status} ({note})")
        elif args.command == "watchlist":
            if args.action == "show":
                _print_watchlist(session)
            elif args.action == "set":
                _report(*set_watchlist(session, args.symbols), verb="Watchlist set to")
            elif args.action == "add":
                _report(*add_to_watchlist(session, args.symbols), verb="Added")
            elif args.action == "remove":
                _report(*remove_from_watchlist(session, args.symbols), verb="Removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
