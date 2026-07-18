"""Yahoo Finance fundamentals for listings that don't file with the SEC.

CSU.TO, AI.PA, RMS.PA, KRI.AT and any on-demand ticker unknown to EDGAR get
no XBRL facts, so their statement/metric dashboards stay empty. This module
fills the gap from the same source that already covers their prices: the
annual and quarterly income statement, balance sheet, and cash flow that
yfinance exposes per ticker (roughly the last 4-5 fiscal years).

Yahoo line labels are mapped onto the *same canonical XBRL tags* the SQL
views (migrations 0002/0004/0006) already understand, and stored in
`fundamentals` under taxonomy 'yahoo'. That way both the Ticker Fundamentals
and Financial Statements dashboards work for these names with no view
changes. Where Yahoo's sign convention differs from the XBRL tag it maps to
(cash outflows are negative at Yahoo, positive under tags like
`PaymentsToAcquirePropertyPlantAndEquipment`), the mapping flips the sign so
mixed SEC/Yahoo data reads consistently.

`facts_from_statement` is a pure function over a statement DataFrame so it
can be unit-tested without network or database access.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from fintracker.db import session_scope
from fintracker.ingest.fundamentals import _upsert_facts
from fintracker.models import Instrument

log = logging.getLogger(__name__)

TAXONOMY = "yahoo"

# Fabricated period lengths for flow facts: Yahoo only reports the period-end
# date, and the SQL views window on (period_end - period_start) — 330..400
# days is annual, 60..120 is quarterly.
ANNUAL_DAYS = 364
QUARTER_DAYS = 91

# (yahoo label, canonical XBRL tag, sign). Order matters: when two Yahoo
# labels map onto the same tag, the first one present for a period wins.
# The tags must stay within the sets the views map (see migration 0006).
INCOME_LINES: tuple[tuple[str, str, int], ...] = (
    ("Total Revenue", "Revenues", 1),
    ("Cost Of Revenue", "CostOfRevenue", 1),
    ("Gross Profit", "GrossProfit", 1),
    ("Research And Development", "ResearchAndDevelopmentExpense", 1),
    ("Selling General And Administration", "SellingGeneralAndAdministrativeExpense", 1),
    ("Selling And Marketing Expense", "SellingAndMarketingExpense", 1),
    ("General And Administrative Expense", "GeneralAndAdministrativeExpense", 1),
    ("Restructuring And Mergern Acquisition", "RestructuringCharges", 1),
    ("Impairment Of Capital Assets", "AssetImpairmentCharges", 1),
    ("Operating Expense", "OperatingExpenses", 1),
    ("Operating Income", "OperatingIncomeLoss", 1),
    ("Interest Income", "InvestmentIncomeInterest", 1),
    ("Interest Expense", "InterestExpense", 1),
    ("Earnings From Equity Interest", "IncomeLossFromEquityMethodInvestments", 1),
    ("Other Non Operating Income Expenses", "OtherNonoperatingIncomeExpense", 1),
    (
        "Pretax Income",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        1,
    ),
    ("Tax Provision", "IncomeTaxExpenseBenefit", 1),
    ("Net Income", "NetIncomeLoss", 1),
    ("Minority Interests", "NetIncomeLossAttributableToNoncontrollingInterest", 1),
    ("Basic EPS", "EarningsPerShareBasic", 1),
    ("Diluted EPS", "EarningsPerShareDiluted", 1),
    ("Basic Average Shares", "WeightedAverageNumberOfSharesOutstandingBasic", 1),
    ("Diluted Average Shares", "WeightedAverageNumberOfDilutedSharesOutstanding", 1),
)

BALANCE_LINES: tuple[tuple[str, str, int], ...] = (
    ("Cash And Cash Equivalents", "CashAndCashEquivalentsAtCarryingValue", 1),
    ("Other Short Term Investments", "ShortTermInvestments", 1),
    ("Accounts Receivable", "AccountsReceivableNetCurrent", 1),
    ("Receivables", "AccountsReceivableNetCurrent", 1),
    ("Inventory", "InventoryNet", 1),
    ("Prepaid Assets", "PrepaidExpenseCurrent", 1),
    ("Other Current Assets", "OtherAssetsCurrent", 1),
    ("Current Assets", "AssetsCurrent", 1),
    ("Net PPE", "PropertyPlantAndEquipmentNet", 1),
    ("Goodwill", "Goodwill", 1),
    ("Other Intangible Assets", "IntangibleAssetsNetExcludingGoodwill", 1),
    ("Investments And Advances", "LongTermInvestments", 1),
    ("Other Non Current Assets", "OtherAssetsNoncurrent", 1),
    ("Total Non Current Assets", "AssetsNoncurrent", 1),
    ("Total Assets", "Assets", 1),
    ("Accounts Payable", "AccountsPayableCurrent", 1),
    ("Payables", "AccountsPayableAndAccruedLiabilitiesCurrent", 1),
    ("Current Accrued Expenses", "AccruedLiabilitiesCurrent", 1),
    ("Current Deferred Revenue", "DeferredRevenueCurrent", 1),
    ("Current Debt", "DebtCurrent", 1),
    ("Current Capital Lease Obligation", "OperatingLeaseLiabilityCurrent", 1),
    ("Other Current Liabilities", "OtherLiabilitiesCurrent", 1),
    ("Current Liabilities", "LiabilitiesCurrent", 1),
    ("Long Term Debt", "LongTermDebtNoncurrent", 1),
    ("Long Term Capital Lease Obligation", "OperatingLeaseLiabilityNoncurrent", 1),
    ("Non Current Deferred Revenue", "DeferredRevenueNoncurrent", 1),
    ("Non Current Deferred Taxes Liabilities", "DeferredIncomeTaxLiabilitiesNet", 1),
    ("Other Non Current Liabilities", "OtherLiabilitiesNoncurrent", 1),
    ("Total Non Current Liabilities Net Minority Interest", "LiabilitiesNoncurrent", 1),
    ("Total Liabilities Net Minority Interest", "Liabilities", 1),
    ("Common Stock", "CommonStockValue", 1),
    ("Additional Paid In Capital", "AdditionalPaidInCapital", 1),
    ("Treasury Stock", "TreasuryStockValue", 1),
    ("Retained Earnings", "RetainedEarningsAccumulatedDeficit", 1),
    (
        "Gains Losses Not Affecting Retained Earnings",
        "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        1,
    ),
    ("Minority Interest", "MinorityInterest", 1),
    ("Stockholders Equity", "StockholdersEquity", 1),
    (
        "Total Equity Gross Minority Interest",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        1,
    ),
)

# Yahoo reports cash outflows as negative values; the XBRL "Payments…" /
# "Repayments…" / "IncreaseDecrease…" tags they map onto are positive, so
# those rows flip the sign.
CASHFLOW_LINES: tuple[tuple[str, str, int], ...] = (
    ("Operating Cash Flow", "NetCashProvidedByUsedInOperatingActivities", 1),
    ("Depreciation And Amortization", "DepreciationAndAmortization", 1),
    ("Stock Based Compensation", "ShareBasedCompensation", 1),
    ("Deferred Income Tax", "DeferredIncomeTaxExpenseBenefit", 1),
    ("Change In Receivables", "IncreaseDecreaseInAccountsReceivable", -1),
    ("Change In Inventory", "IncreaseDecreaseInInventories", -1),
    ("Change In Payable", "IncreaseDecreaseInAccountsPayable", 1),
    (
        "Change In Payables And Accrued Expense",
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities",
        1,
    ),
    ("Investing Cash Flow", "NetCashProvidedByUsedInInvestingActivities", 1),
    ("Capital Expenditure", "PaymentsToAcquirePropertyPlantAndEquipment", -1),
    ("Purchase Of Business", "PaymentsToAcquireBusinessesNetOfCashAcquired", -1),
    ("Purchase Of Investment", "PaymentsToAcquireInvestments", -1),
    ("Sale Of Investment", "ProceedsFromSaleMaturityAndCollectionsOfInvestments", 1),
    ("Financing Cash Flow", "NetCashProvidedByUsedInFinancingActivities", 1),
    ("Issuance Of Debt", "ProceedsFromIssuanceOfLongTermDebt", 1),
    ("Repayment Of Debt", "RepaymentsOfDebt", -1),
    ("Issuance Of Capital Stock", "ProceedsFromIssuanceOfCommonStock", 1),
    ("Common Stock Issuance", "ProceedsFromIssuanceOfCommonStock", 1),
    ("Repurchase Of Capital Stock", "PaymentsForRepurchaseOfCommonStock", -1),
    ("Cash Dividends Paid", "PaymentsOfDividends", -1),
    ("Common Stock Dividend Paid", "PaymentsOfDividendsCommonStock", -1),
    ("Effect Of Exchange Rate Changes", "EffectOfExchangeRateOnCashAndCashEquivalents", 1),
    ("Changes In Cash", "CashAndCashEquivalentsPeriodIncreaseDecrease", 1),
)

_PER_SHARE_TAGS = frozenset({"EarningsPerShareBasic", "EarningsPerShareDiluted"})
_SHARE_COUNT_TAGS = frozenset(
    {
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    }
)


def _unit_for(tag: str, currency: str) -> str:
    if tag in _SHARE_COUNT_TAGS:
        return "shares"
    if tag in _PER_SHARE_TAGS:
        return f"{currency}/shares"
    return currency


def facts_from_statement(
    frame: pd.DataFrame,
    lines: Sequence[tuple[str, str, int]],
    currency: str,
    *,
    instant: bool,
    quarterly: bool = False,
) -> list[dict[str, Any]]:
    """Normalize one yfinance statement DataFrame into fundamentals rows.

    The frame is line-label × period-end. Balance-sheet facts are instant
    (period_start == period_end); flow facts get a fabricated period_start
    (end - 364/91 days) that lands inside the annual/quarterly windows the
    SQL views select on. NaN cells are skipped; when several Yahoo labels
    map to one tag, the first listed wins per period.
    """
    if frame is None or frame.empty:
        return []
    out: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for label, tag, sign in lines:
        if label not in frame.index:
            continue
        row = frame.loc[label]
        # Duplicate labels would return a DataFrame; keep the first row.
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        for col, value in row.items():
            if value is None or pd.isna(value):
                continue
            try:
                period_end = pd.Timestamp(col).date()
            except (TypeError, ValueError):
                continue
            if (tag, period_end) in out:
                continue
            if instant:
                period_start = period_end
            else:
                days = QUARTER_DAYS if quarterly else ANNUAL_DAYS
                period_start = period_end - dt.timedelta(days=days)
            out[(tag, period_end)] = {
                "taxonomy": TAXONOMY,
                "tag": tag,
                "unit": _unit_for(tag, currency),
                "period_start": period_start,
                "period_end": period_end,
                "value": sign * float(value),
                "fiscal_year": period_end.year,
                "fiscal_period": "Q" if quarterly and not instant else "FY",
                "form": TAXONOMY,
                "accession_no": None,
                "filed_at": None,
            }
    return list(out.values())


def _fetch_frame(ticker: Any, attr: str) -> pd.DataFrame:
    try:
        frame = getattr(ticker, attr)
    except Exception:
        log.warning("Yahoo statement %r unavailable", attr, exc_info=True)
        return pd.DataFrame()
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _financial_currency(ticker: Any) -> str | None:
    """Statements can be reported in a different currency than the listing
    trades in (e.g. CSU.TO trades in CAD but reports in USD)."""
    try:
        currency = ticker.info.get("financialCurrency")
    except Exception:
        return None
    return str(currency).upper() if currency else None


def ingest_instrument_yahoo_facts(session: Session, inst: Instrument) -> int:
    """Fetch all six statements for one instrument and upsert the facts."""
    import yfinance as yf

    assert inst.yahoo_symbol is not None
    ticker = yf.Ticker(inst.yahoo_symbol)
    currency = _financial_currency(ticker) or inst.currency

    facts: list[dict[str, Any]] = []
    for attr, lines, instant, quarterly in (
        ("income_stmt", INCOME_LINES, False, False),
        ("quarterly_income_stmt", INCOME_LINES, False, True),
        ("balance_sheet", BALANCE_LINES, True, False),
        ("quarterly_balance_sheet", BALANCE_LINES, True, True),
        ("cashflow", CASHFLOW_LINES, False, False),
        ("quarterly_cashflow", CASHFLOW_LINES, False, True),
    ):
        frame = _fetch_frame(ticker, attr)
        facts.extend(
            facts_from_statement(frame, lines, currency, instant=instant, quarterly=quarterly)
        )

    _upsert_facts(session, inst.id, facts)
    log.info("Upserted %d Yahoo facts for %s.", len(facts), inst.symbol)
    return len(facts)


def ingest_yahoo_fundamentals() -> None:
    """Daily job: statements for every equity without SEC XBRL coverage."""
    with session_scope() as session:
        instruments = (
            session.execute(
                select(Instrument).where(
                    Instrument.kind == "equity",
                    Instrument.taxonomy.is_(None),
                    Instrument.yahoo_symbol.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        for inst in instruments:
            try:
                ingest_instrument_yahoo_facts(session, inst)
            except Exception:
                log.exception("Yahoo fundamentals ingest failed for %s", inst.symbol)
