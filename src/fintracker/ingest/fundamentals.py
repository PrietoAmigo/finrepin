"""SEC XBRL fundamentals.

The parsing helpers (`resolve_cik`, `iter_recent_filings`, `select_new_filings`,
`extract_facts`) are pure functions over the SEC JSON payloads so they can be
unit-tested without network or database access. `ingest_fundamentals` is the
scheduled orchestrator: it detects new filings per instrument via the
submissions feed, logs them in `filings`, and upserts curated facts into
`fundamentals`. Facts are re-extracted on every run — not only when a new
filing appears — so additions to CURATED_TAGS backfill their full history
on the next run (one extra companyfacts request per instrument per run).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fintracker.config import get_settings
from fintracker.db import session_scope
from fintracker.ingest.sec_client import SecClient
from fintracker.models import Filing, Fundamental, Instrument

log = logging.getLogger(__name__)

# Filing forms that carry the financial statements we care about.
FORMS_WITH_FINANCIALS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K"}
)

# Curated tags per taxonomy — the fundamentals we store, nothing more.
# The Grafana metric views (migration 0002) group these into metric families
# (revenue, eps, op_income, ocf, capex, shares, debt); keep both in sync.
CURATED_TAGS: dict[str, tuple[str, ...]] = {
    "us-gaap": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "NetIncomeLoss",
        "OperatingIncomeLoss",
        "EarningsPerShareDiluted",
        "Assets",
        "Liabilities",
        "StockholdersEquity",
        "CashAndCashEquivalentsAtCarryingValue",
        # Cash flow: free cash flow = operating cash flow - capex.
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        # Share count for per-share ratios (P/FCF).
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        # Debt components (instant facts).
        "LongTermDebt",
        "LongTermDebtCurrent",
        "LongTermDebtNoncurrent",
        "DebtCurrent",
        "ShortTermBorrowings",
    ),
    "ifrs-full": (
        "Revenue",
        "ProfitLoss",
        "ProfitLossAttributableToOwnersOfParent",
        "Assets",
        "Liabilities",
        "Equity",
        "CashAndCashEquivalents",
        "DilutedEarningsLossPerShare",
        "ProfitLossFromOperatingActivities",
        "CashFlowsFromUsedInOperatingActivities",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "AdjustedWeightedAverageShares",
        "WeightedAverageShares",
        "Borrowings",
        "LongtermBorrowings",
        "ShorttermBorrowings",
        "CurrentPortionOfLongtermBorrowings",
    ),
}


def resolve_cik(ticker: str, company_tickers: dict[str, Any]) -> str | None:
    """Find a ticker in company_tickers.json; returns a 10-digit zero-padded CIK.

    The file maps arbitrary numeric keys to {cik_str, ticker, title}.
    """
    want = ticker.upper()
    for entry in company_tickers.values():
        if str(entry.get("ticker", "")).upper() == want:
            return f"{int(entry['cik_str']):010d}"
    return None


def iter_recent_filings(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the parallel arrays of the submissions feed into dicts."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filed = recent.get("filingDate", [])
    return [
        {"accession_no": accession, "form": form, "filed_at": dt.date.fromisoformat(date)}
        for accession, form, date in zip(accessions, forms, filed, strict=False)
        if accession and form and date
    ]


def select_new_filings(
    submissions: dict[str, Any],
    known_accessions: Iterable[str],
    forms: frozenset[str] = FORMS_WITH_FINANCIALS,
) -> list[dict[str, Any]]:
    """Financial filings from the feed that we haven't processed yet."""
    known = set(known_accessions)
    return [
        filing
        for filing in iter_recent_filings(submissions)
        if filing["form"] in forms and filing["accession_no"] not in known
    ]


def extract_facts(
    company_facts: dict[str, Any], taxonomy: str, tags: Sequence[str]
) -> list[dict[str, Any]]:
    """Normalize curated facts from a companyfacts payload.

    Instant facts (balance-sheet items) have no `start`; they are stored with
    period_start == period_end so the uniqueness key never contains NULLs.
    Duplicate (tag, unit, period) entries collapse to the most recently filed.
    """
    facts = company_facts.get("facts", {}).get(taxonomy, {})
    out: dict[tuple[str, str, dt.date, dt.date], dict[str, Any]] = {}
    for tag in tags:
        entry = facts.get(tag)
        if not entry:
            continue
        for unit, items in entry.get("units", {}).items():
            for item in items:
                end_raw, value = item.get("end"), item.get("val")
                if end_raw is None or value is None:
                    continue
                period_end = dt.date.fromisoformat(end_raw)
                start_raw = item.get("start")
                period_start = dt.date.fromisoformat(start_raw) if start_raw else period_end
                filed_raw = item.get("filed")
                fact = {
                    "taxonomy": taxonomy,
                    "tag": tag,
                    "unit": unit,
                    "period_start": period_start,
                    "period_end": period_end,
                    "value": value,
                    "fiscal_year": item.get("fy"),
                    "fiscal_period": item.get("fp"),
                    "form": item.get("form"),
                    "accession_no": item.get("accn"),
                    "filed_at": dt.date.fromisoformat(filed_raw) if filed_raw else None,
                }
                key = (tag, unit, period_start, period_end)
                previous = out.get(key)
                if previous is None or (fact["filed_at"] or dt.date.min) >= (
                    previous["filed_at"] or dt.date.min
                ):
                    out[key] = fact
    return list(out.values())


def _upsert_facts(session: Session, instrument_id: int, facts: list[dict[str, Any]]) -> None:
    for fact in facts:
        stmt = pg_insert(Fundamental).values(instrument_id=instrument_id, **fact)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_fundamentals_fact",
                set_={
                    "value": stmt.excluded.value,
                    "fiscal_year": stmt.excluded.fiscal_year,
                    "fiscal_period": stmt.excluded.fiscal_period,
                    "form": stmt.excluded.form,
                    "accession_no": stmt.excluded.accession_no,
                    "filed_at": stmt.excluded.filed_at,
                },
            )
        )


def ingest_fundamentals() -> None:
    settings = get_settings()
    if not settings.sec_user_agent:
        log.warning("SEC_USER_AGENT not set — skipping fundamentals ingest.")
        return

    client = SecClient()
    company_tickers: dict[str, Any] | None = None

    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "equity", Instrument.taxonomy.is_not(None)
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            try:
                if not inst.cik:
                    if company_tickers is None:
                        company_tickers = client.company_tickers()
                    cik = resolve_cik(inst.symbol, company_tickers)
                    if cik is None:
                        log.warning("Could not resolve a CIK for %s — skipping.", inst.symbol)
                        continue
                    inst.cik = cik
                    log.info("Resolved CIK for %s: %s", inst.symbol, cik)

                known = set(
                    session.scalars(
                        select(Filing.accession_no).where(Filing.instrument_id == inst.id)
                    )
                )
                new_filings = select_new_filings(client.submissions(inst.cik), known)
                for filing in new_filings:
                    session.add(Filing(instrument_id=inst.id, **filing))
                if new_filings:
                    log.info(
                        "Found %d new filing(s) for %s: %s",
                        len(new_filings),
                        inst.symbol,
                        ", ".join(f["form"] for f in new_filings),
                    )
                else:
                    log.info("No new filings for %s — refreshing facts anyway.", inst.symbol)

                assert inst.taxonomy is not None
                facts = extract_facts(
                    client.company_facts(inst.cik), inst.taxonomy, CURATED_TAGS[inst.taxonomy]
                )
                _upsert_facts(session, inst.id, facts)
                log.info("Upserted %d facts for %s.", len(facts), inst.symbol)
            except Exception:
                log.exception("Fundamentals ingest failed for %s", inst.symbol)
