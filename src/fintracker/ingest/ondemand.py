"""On-demand ticker requests from the Grafana dashboard search box.

The Ticker Fundamentals dashboard INSERTs typed symbols into
`ticker_requests` (via a data-modifying CTE in its status-panel query).
`process_ticker_requests` runs from a minutely scheduler job: it validates
each pending symbol against SEC EDGAR and Yahoo Finance, and — when the
ticker exists — registers the instrument, backfills its full price history,
and ingests fundamentals: SEC XBRL facts when the company files with the
SEC, Yahoo Finance statements otherwise. Unknown symbols are marked
`not_found` and nothing else happens.

Rows are kept after processing (status: done / not_found / error) so the
dashboard's insert-on-refresh stays idempotent; delete a row to retry it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.ingest.fundamentals import ingest_instrument_facts, resolve_cik
from fintracker.ingest.prices import fetch_daily_history, rows_from_history, upsert_price_rows
from fintracker.ingest.sec_client import SecClient
from fintracker.ingest.yahoo_fundamentals import ingest_instrument_yahoo_facts
from fintracker.models import Instrument, TickerRequest

log = logging.getLogger(__name__)

# Uppercase Yahoo-style symbols: letters/digits plus exchange suffixes (BN,
# CSU.TO, EURUSD=X, BRK-B). The dashboard query applies the same pattern, so
# anything that reaches `pending` should already conform.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.=-]{0,31}$")


def normalize_symbol(raw: str) -> str | None:
    """Uppercased, trimmed symbol — or None when it can't be a ticker."""
    symbol = raw.strip().upper()
    return symbol if _SYMBOL_RE.fullmatch(symbol) else None


def detect_taxonomy(company_facts: dict[str, Any]) -> str | None:
    """Which supported XBRL taxonomy a companyfacts payload reports under."""
    facts = company_facts.get("facts", {})
    for taxonomy in ("us-gaap", "ifrs-full"):
        if facts.get(taxonomy):
            return taxonomy
    return None


def _fetch_currency(yahoo_symbol: str) -> str | None:
    import yfinance as yf

    try:
        currency = yf.Ticker(yahoo_symbol).fast_info["currency"]
    except Exception:
        return None
    return str(currency).upper() if currency else None


def _resolve(req: TickerRequest, session: Session) -> tuple[str, str | None]:
    """Do the work for one request; returns (status, note)."""
    symbol = normalize_symbol(req.symbol)
    if symbol is None:
        return "not_found", "not a valid ticker symbol"

    if session.scalar(select(Instrument).where(Instrument.symbol == symbol)) is not None:
        return "done", "already tracked"

    # Yahoo is the price source and covers non-SEC listings too.
    try:
        price_rows = rows_from_history(fetch_daily_history(symbol))
    except Exception:
        log.exception("Yahoo lookup failed for %s", symbol)
        price_rows = []

    # SEC: known ticker there means fundamentals coverage.
    cik: str | None = None
    taxonomy: str | None = None
    sec_name: str | None = None
    company_facts: dict[str, Any] | None = None
    client: SecClient | None = None
    if get_settings().sec_user_agent:
        client = SecClient()
        cik = resolve_cik(symbol, client.company_tickers())
    if client is not None and cik is not None:
        company_facts = client.company_facts(cik)
        taxonomy = detect_taxonomy(company_facts)
        sec_name = str(company_facts.get("entityName") or "") or None

    if not price_rows and cik is None:
        return "not_found", "unknown to both Yahoo Finance and SEC EDGAR"

    inst = Instrument(
        symbol=symbol,
        name=sec_name or symbol,
        kind="equity",
        currency=_fetch_currency(symbol) or "USD",
        yahoo_symbol=symbol if price_rows else None,
        cik=cik,
        taxonomy=taxonomy,
    )
    session.add(inst)
    session.flush()

    notes = []
    if price_rows:
        upsert_price_rows(session, inst.id, price_rows, source="yfinance")
        notes.append(f"{len(price_rows)} price rows")
    if client is not None and cik is not None and taxonomy is not None:
        n_facts = ingest_instrument_facts(session, client, inst, company_facts=company_facts)
        notes.append(f"{n_facts} SEC fundamentals facts")
    elif price_rows:
        # No SEC coverage — pull statements from Yahoo instead.
        n_facts = ingest_instrument_yahoo_facts(session, inst)
        notes.append(f"{n_facts} Yahoo fundamentals facts")
    else:
        notes.append("no fundamentals")
    return "done", ", ".join(notes)


def process_ticker_requests() -> None:
    """Process every pending row; one bad request must not block the rest."""
    with session_scope() as session:
        pending_ids = session.scalars(
            select(TickerRequest.id)
            .where(TickerRequest.status == "pending")
            .order_by(TickerRequest.requested_at)
        ).all()
    if not pending_ids:
        return

    for req_id in pending_ids:
        # One transaction per request so a failure can't poison the others.
        with session_scope() as session:
            req = session.get(TickerRequest, req_id)
            if req is None or req.status != "pending":
                continue
            log.info("Processing ticker request %r", req.symbol)
            try:
                status, note = _resolve(req, session)
            except Exception:
                log.exception("Ticker request failed for %r", req.symbol)
                session.rollback()
                req = session.get(TickerRequest, req_id)
                assert req is not None
                status, note = "error", "ingest failed — see app logs"
            req.status = status
            req.note = note
            req.processed_at = dt.datetime.now(dt.UTC)
            log.info("Ticker request %r -> %s (%s)", req.symbol, status, note)
