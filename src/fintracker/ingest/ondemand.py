"""On-demand instrument requests from the Grafana Manage dashboard.

The Manage dashboard's Add-ticker form INSERTs typed symbols into
`ticker_requests`. `process_ticker_requests` runs from a minutely scheduler
job and, for each pending symbol:

* **Cryptocurrencies** (e.g. XMR) — detected via Yahoo's quoteType — register
  as `kind='crypto'` with the `<SYM>-USD` Yahoo pair for daily history and a
  best-effort CoinGecko id for the live spot, exactly like the seeded BTC/ETH.
* **Everything else** is validated against SEC EDGAR and Yahoo Finance and, when
  it exists, registers as an equity: full Yahoo price history plus fundamentals
  (SEC XBRL facts when the company files with the SEC, Yahoo statements
  otherwise).

Unknown symbols are marked `not_found`. Rows are kept after processing
(status: done / not_found / error) so re-adding stays idempotent; delete a row
to retry it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests
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


def _fetch_sector_region(yahoo_symbol: str) -> tuple[str | None, str | None]:
    """Best-effort (sector, region) for the portfolio allocation panels.

    Yahoo's `info` carries GICS-ish `sector` and the listing's `country` for
    equities; both are absent for ETFs/indexes/crypto/etc., which is fine —
    the allocation views simply group those under NULL.
    """
    import yfinance as yf

    try:
        info = yf.Ticker(yahoo_symbol).info
    except Exception:
        return None, None
    if not info:
        return None, None
    sector = info.get("sector")
    region = info.get("country")
    return (str(sector) if sector else None, str(region) if region else None)


COINGECKO_COINS_LIST = "https://api.coingecko.com/api/v3/coins/list"


@dataclass(frozen=True)
class YahooMeta:
    quote_type: str
    name: str
    currency: str | None


@dataclass(frozen=True)
class CryptoSpec:
    symbol: str  # instrument symbol, e.g. "XMR" (base, no -USD)
    yahoo_symbol: str  # Yahoo pair, e.g. "XMR-USD"
    name: str
    coingecko_id: str | None


_BARE_SYMBOL_RE = re.compile(r"^[A-Z0-9]+$")


def _is_bare_symbol(symbol: str) -> bool:
    """A plain coin ticker with no exchange suffix, so `<SYM>-USD` is worth a probe."""
    return bool(_BARE_SYMBOL_RE.fullmatch(symbol))


def _strip_usd(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("-USD") else symbol


def _clean_crypto_name(name: str) -> str:
    """Yahoo names crypto pairs like 'Monero USD'; drop the quote-currency tail."""
    return name[:-4].rstrip() if name.upper().endswith(" USD") else name.strip()


def _yahoo_meta(yahoo_symbol: str) -> YahooMeta | None:
    """Yahoo's quoteType/name/currency for a symbol, or None if Yahoo doesn't know it."""
    import yfinance as yf

    try:
        info = yf.Ticker(yahoo_symbol).info
    except Exception:
        return None
    quote_type = info.get("quoteType") if info else None
    if not quote_type:
        return None
    name = info.get("shortName") or info.get("longName") or ""
    currency = (info.get("currency") or "").upper() or None
    return YahooMeta(quote_type=str(quote_type).upper(), name=str(name), currency=currency)


@lru_cache(maxsize=1)
def _coingecko_coins() -> tuple[dict[str, str], ...]:
    """CoinGecko's full id/symbol/name coin list, cached for the process lifetime."""
    resp = requests.get(COINGECKO_COINS_LIST, timeout=30)
    resp.raise_for_status()
    return tuple(
        {"id": c.get("id", ""), "symbol": c.get("symbol", ""), "name": c.get("name", "")}
        for c in resp.json()
    )


def match_coingecko_id(symbol: str, name: str, coins: Sequence[dict[str, str]]) -> str | None:
    """Best-effort CoinGecko id for a coin ticker; None when absent or ambiguous.

    Pure so it can be unit-tested. Ticker symbols collide across coins, so a
    unique symbol match wins; otherwise the coin name disambiguates; otherwise
    None (the coin still gets Yahoo history — only the fresher CoinGecko spot is
    skipped).
    """
    sym = symbol.lower()
    matches = [c for c in coins if c["symbol"].lower() == sym]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]["id"] or None
    wanted = name.lower()
    for c in matches:
        if c["name"].lower() == wanted:
            return c["id"] or None
    for c in matches:
        cn = c["name"].lower()
        if cn and (cn in wanted or wanted in cn):
            return c["id"] or None
    return None


def _resolve_coingecko_id(symbol: str, name: str) -> str | None:
    try:
        return match_coingecko_id(symbol, name, _coingecko_coins())
    except Exception:
        log.warning("CoinGecko coin-list lookup failed for %s", symbol, exc_info=True)
        return None


def _crypto_spec(base: str, yahoo_symbol: str, meta: YahooMeta) -> CryptoSpec:
    name = _clean_crypto_name(meta.name) or base
    return CryptoSpec(
        symbol=base,
        yahoo_symbol=yahoo_symbol,
        name=name,
        coingecko_id=_resolve_coingecko_id(base, name),
    )


def detect_crypto(symbol: str) -> CryptoSpec | None:
    """Classify a requested symbol as a cryptocurrency via Yahoo's quoteType.

    A symbol Yahoo already reports as crypto (e.g. BTC-USD) is taken as-is; a
    bare ticker Yahoo does not recognise at all (e.g. XMR) is retried as
    `<SYM>-USD`. Anything Yahoo knows as an equity/ETF/index/currency is left to
    the equity path, so only symbols Yahoo can't place as equities are probed as
    coin pairs.
    """
    meta = _yahoo_meta(symbol)
    if meta is not None:
        if meta.quote_type == "CRYPTOCURRENCY":
            return _crypto_spec(_strip_usd(symbol), symbol, meta)
        return None  # Yahoo knows it as something else — not crypto.
    if _is_bare_symbol(symbol):
        pair = f"{symbol}-USD"
        pair_meta = _yahoo_meta(pair)
        if pair_meta is not None and pair_meta.quote_type == "CRYPTOCURRENCY":
            return _crypto_spec(symbol, pair, pair_meta)
    return None


def _register_crypto(session: Session, spec: CryptoSpec) -> tuple[str, str]:
    """Register a detected crypto and backfill its Yahoo history."""
    try:
        price_rows = rows_from_history(fetch_daily_history(spec.yahoo_symbol))
    except Exception:
        log.exception("Yahoo history failed for %s", spec.yahoo_symbol)
        price_rows = []
    if not price_rows:
        return "not_found", f"no Yahoo price history for {spec.yahoo_symbol}"
    inst = Instrument(
        symbol=spec.symbol,
        name=spec.name,
        kind="crypto",
        currency="USD",
        yahoo_symbol=spec.yahoo_symbol,
        coingecko_id=spec.coingecko_id,
    )
    session.add(inst)
    session.flush()
    upsert_price_rows(session, inst.id, price_rows, source="yfinance")
    spot = f"CoinGecko spot ({spec.coingecko_id})" if spec.coingecko_id else "no CoinGecko spot"
    return "done", f"crypto · {len(price_rows)} price rows, {spot}"


def _resolve(req: TickerRequest, session: Session) -> tuple[str, str | None]:
    """Do the work for one request; returns (status, note)."""
    symbol = normalize_symbol(req.symbol)
    if symbol is None:
        return "not_found", "not a valid ticker symbol"

    if session.scalar(select(Instrument).where(Instrument.symbol == symbol)) is not None:
        return "done", "already tracked"

    # Cryptocurrencies register as kind='crypto' (Yahoo history + CoinGecko spot),
    # not as equities, so the same Add-ticker box handles coins like XMR.
    crypto = detect_crypto(symbol)
    if crypto is not None:
        if crypto.symbol != symbol and (
            session.scalar(select(Instrument).where(Instrument.symbol == crypto.symbol)) is not None
        ):
            return "done", "already tracked"
        return _register_crypto(session, crypto)

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

    sector, region = _fetch_sector_region(symbol) if price_rows else (None, None)
    inst = Instrument(
        symbol=symbol,
        name=sec_name or symbol,
        kind="equity",
        currency=_fetch_currency(symbol) or "USD",
        sector=sector,
        region=region,
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


def enrich_sector_region(session: Session) -> int:
    """Best-effort backfill of sector/region for equities that predate those
    columns (or were added before a Yahoo lookup succeeded). Returns how many
    instruments were updated. Safe to re-run — only NULL/NULL rows are
    touched, and a lookup failure just leaves them NULL for the next run."""
    instruments = session.scalars(
        select(Instrument).where(
            Instrument.kind == "equity",
            Instrument.yahoo_symbol.is_not(None),
            Instrument.sector.is_(None),
            Instrument.region.is_(None),
        )
    ).all()
    updated = 0
    for inst in instruments:
        assert inst.yahoo_symbol is not None
        sector, region = _fetch_sector_region(inst.yahoo_symbol)
        if sector or region:
            inst.sector = sector
            inst.region = region
            updated += 1
    return updated


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
