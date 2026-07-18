"""Annual statement views backing the Financial Statements dashboard.

Two plain views over `fundamentals`:

- equity_statement_annual — curated tags mapped onto income-statement /
                            balance-sheet / cash-flow line items, one value
                            per line per fiscal year. Flow items take the
                            annual-duration fact; balance-sheet items take
                            the instant fact at a fiscal-year end.
- equity_statement_matrix — the same data densified to the full
                            line-item x fiscal-year grid per statement, so
                            Grafana's grouping-to-matrix transformation
                            renders complete, consistently ordered tables.

Fiscal years are labeled by the calendar year the fiscal year ends in
(a fiscal year ending 2025-01-31 shows as 2025).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-18

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Maps raw XBRL tags (us-gaap and ifrs-full) to statement line items, with a
# display order and a priority to break ties when a filer reports several tags
# for the same line and period. Keep in sync with CURATED_TAGS in
# fintracker.ingest.fundamentals.
EQUITY_STATEMENT_ANNUAL = """
CREATE VIEW equity_statement_annual AS
WITH mapping(statement, ord, line, priority, tag) AS (VALUES
    ('income',  1, 'Revenue', 1, 'RevenueFromContractWithCustomerExcludingAssessedTax'),
    ('income',  1, 'Revenue', 2, 'Revenues'),
    ('income',  1, 'Revenue', 3, 'Revenue'),
    ('income',  2, 'Cost of revenue', 1, 'CostOfRevenue'),
    ('income',  2, 'Cost of revenue', 2, 'CostOfGoodsAndServicesSold'),
    ('income',  2, 'Cost of revenue', 3, 'CostOfSales'),
    ('income',  3, 'Gross profit', 1, 'GrossProfit'),
    ('income',  4, 'R&D expense', 1, 'ResearchAndDevelopmentExpense'),
    ('income',  5, 'SG&A expense', 1, 'SellingGeneralAndAdministrativeExpense'),
    ('income',  6, 'Operating expenses', 1, 'OperatingExpenses'),
    ('income',  6, 'Operating expenses', 2, 'OperatingExpense'),
    ('income',  7, 'Operating income', 1, 'OperatingIncomeLoss'),
    ('income',  7, 'Operating income', 2, 'ProfitLossFromOperatingActivities'),
    ('income',  8, 'Interest expense', 1, 'InterestExpense'),
    ('income',  8, 'Interest expense', 2, 'InterestExpenseNonoperating'),
    ('income',  8, 'Interest expense', 3, 'FinanceCosts'),
    ('income',  9, 'Pretax income', 1,
     'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'),
    ('income',  9, 'Pretax income', 2,
     'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments'),
    ('income',  9, 'Pretax income', 3, 'ProfitLossBeforeTax'),
    ('income', 10, 'Income tax expense', 1, 'IncomeTaxExpenseBenefit'),
    ('income', 10, 'Income tax expense', 2, 'IncomeTaxExpenseContinuingOperations'),
    ('income', 11, 'Net income', 1, 'NetIncomeLoss'),
    ('income', 11, 'Net income', 2, 'ProfitLossAttributableToOwnersOfParent'),
    ('income', 11, 'Net income', 3, 'ProfitLoss'),
    ('income', 12, 'EPS (basic)', 1, 'EarningsPerShareBasic'),
    ('income', 12, 'EPS (basic)', 2, 'BasicEarningsLossPerShare'),
    ('income', 13, 'EPS (diluted)', 1, 'EarningsPerShareDiluted'),
    ('income', 13, 'EPS (diluted)', 2, 'DilutedEarningsLossPerShare'),

    ('balance',  1, 'Cash & equivalents', 1, 'CashAndCashEquivalentsAtCarryingValue'),
    ('balance',  1, 'Cash & equivalents', 2, 'CashAndCashEquivalents'),
    ('balance',  2, 'Short-term investments', 1, 'ShortTermInvestments'),
    ('balance',  2, 'Short-term investments', 2, 'MarketableSecuritiesCurrent'),
    ('balance',  3, 'Receivables', 1, 'AccountsReceivableNetCurrent'),
    ('balance',  3, 'Receivables', 2, 'TradeAndOtherCurrentReceivables'),
    ('balance',  4, 'Inventory', 1, 'InventoryNet'),
    ('balance',  4, 'Inventory', 2, 'Inventories'),
    ('balance',  5, 'Total current assets', 1, 'AssetsCurrent'),
    ('balance',  5, 'Total current assets', 2, 'CurrentAssets'),
    ('balance',  6, 'Property, plant & equipment', 1, 'PropertyPlantAndEquipmentNet'),
    ('balance',  6, 'Property, plant & equipment', 2, 'PropertyPlantAndEquipment'),
    ('balance',  7, 'Goodwill', 1, 'Goodwill'),
    ('balance',  8, 'Intangible assets', 1, 'IntangibleAssetsNetExcludingGoodwill'),
    ('balance',  8, 'Intangible assets', 2, 'IntangibleAssetsOtherThanGoodwill'),
    ('balance',  9, 'Total assets', 1, 'Assets'),
    ('balance', 10, 'Accounts payable', 1, 'AccountsPayableCurrent'),
    ('balance', 10, 'Accounts payable', 2, 'TradeAndOtherCurrentPayables'),
    ('balance', 11, 'Total current liabilities', 1, 'LiabilitiesCurrent'),
    ('balance', 11, 'Total current liabilities', 2, 'CurrentLiabilities'),
    ('balance', 12, 'Long-term debt', 1, 'LongTermDebtNoncurrent'),
    ('balance', 12, 'Long-term debt', 2, 'LongtermBorrowings'),
    ('balance', 12, 'Long-term debt', 3, 'LongTermDebt'),
    ('balance', 13, 'Total liabilities', 1, 'Liabilities'),
    ('balance', 14, 'Retained earnings', 1, 'RetainedEarningsAccumulatedDeficit'),
    ('balance', 14, 'Retained earnings', 2, 'RetainedEarnings'),
    ('balance', 15, 'Total equity', 1, 'StockholdersEquity'),
    ('balance', 15, 'Total equity', 2, 'EquityAttributableToOwnersOfParent'),
    ('balance', 15, 'Total equity', 3,
     'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'),
    ('balance', 15, 'Total equity', 4, 'Equity'),

    ('cashflow', 1, 'Operating cash flow', 1, 'NetCashProvidedByUsedInOperatingActivities'),
    ('cashflow', 1, 'Operating cash flow', 2,
     'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'),
    ('cashflow', 1, 'Operating cash flow', 3, 'CashFlowsFromUsedInOperatingActivities'),
    ('cashflow', 2, 'Depreciation & amortization', 1, 'DepreciationDepletionAndAmortization'),
    ('cashflow', 2, 'Depreciation & amortization', 2, 'DepreciationAndAmortisationExpense'),
    ('cashflow', 3, 'Share-based compensation', 1, 'ShareBasedCompensation'),
    ('cashflow', 4, 'Capital expenditure', 1, 'PaymentsToAcquirePropertyPlantAndEquipment'),
    ('cashflow', 4, 'Capital expenditure', 2, 'PaymentsToAcquireProductiveAssets'),
    ('cashflow', 4, 'Capital expenditure', 3,
     'PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'),
    ('cashflow', 5, 'Investing cash flow', 1, 'NetCashProvidedByUsedInInvestingActivities'),
    ('cashflow', 5, 'Investing cash flow', 2,
     'NetCashProvidedByUsedInInvestingActivitiesContinuingOperations'),
    ('cashflow', 5, 'Investing cash flow', 3, 'CashFlowsFromUsedInInvestingActivities'),
    ('cashflow', 6, 'Dividends paid', 1, 'PaymentsOfDividends'),
    ('cashflow', 6, 'Dividends paid', 2, 'PaymentsOfDividendsCommonStock'),
    ('cashflow', 6, 'Dividends paid', 3, 'DividendsPaidClassifiedAsFinancingActivities'),
    ('cashflow', 7, 'Share buybacks', 1, 'PaymentsForRepurchaseOfCommonStock'),
    ('cashflow', 8, 'Financing cash flow', 1, 'NetCashProvidedByUsedInFinancingActivities'),
    ('cashflow', 8, 'Financing cash flow', 2,
     'NetCashProvidedByUsedInFinancingActivitiesContinuingOperations'),
    ('cashflow', 8, 'Financing cash flow', 3, 'CashFlowsFromUsedInFinancingActivities'),
    ('cashflow', 9, 'Net change in cash', 1,
     'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect'),
    ('cashflow', 9, 'Net change in cash', 2, 'CashAndCashEquivalentsPeriodIncreaseDecrease'),
    ('cashflow', 9, 'Net change in cash', 3, 'IncreaseDecreaseInCashAndCashEquivalents')
),
fy_ends AS (
    SELECT DISTINCT instrument_id, period_end
    FROM fundamentals
    WHERE (period_end - period_start) BETWEEN 330 AND 400
),
mapped AS (
    SELECT f.instrument_id, m.statement, m.ord, m.line, m.priority,
           f.period_end, f.unit, f.filed_at,
           (f.period_end - f.period_start) AS period_days,
           f.value::float8 AS value
    FROM fundamentals f
    JOIN mapping m ON m.tag = f.tag
)
SELECT DISTINCT ON (instrument_id, statement, line, period_end)
       instrument_id, statement, ord, line, period_end,
       EXTRACT(YEAR FROM period_end)::int AS fiscal_year,
       value
FROM mapped
WHERE (statement IN ('income', 'cashflow') AND period_days BETWEEN 330 AND 400)
   OR (statement = 'balance' AND period_days = 0
       AND EXISTS (
           SELECT 1 FROM fy_ends e
           WHERE e.instrument_id = mapped.instrument_id
             AND e.period_end = mapped.period_end
       ))
ORDER BY instrument_id, statement, line, period_end,
         priority,
         CASE WHEN unit IN ('USD', 'USD/shares', 'shares') THEN 0 ELSE 1 END,
         filed_at DESC NULLS LAST
"""

# Dense line-item x fiscal-year grid. Grafana's grouping-to-matrix orders rows
# and columns by first appearance, so gaps in the sparse view would scramble
# the layout; emitting every combination (value NULL where the filer didn't
# tag the line) keeps rows in statement order and years in sequence.
EQUITY_STATEMENT_MATRIX = """
CREATE VIEW equity_statement_matrix AS
WITH years AS (
    SELECT DISTINCT instrument_id, statement, fiscal_year
    FROM equity_statement_annual
),
lines AS (
    SELECT DISTINCT instrument_id, statement, ord, line
    FROM equity_statement_annual
)
SELECT l.instrument_id, l.statement, l.ord, l.line, y.fiscal_year, a.value
FROM lines l
JOIN years y
  ON y.instrument_id = l.instrument_id
 AND y.statement = l.statement
LEFT JOIN equity_statement_annual a
  ON a.instrument_id = l.instrument_id
 AND a.statement = l.statement
 AND a.line = l.line
 AND a.fiscal_year = y.fiscal_year
"""

# In dependency order; downgrade drops them in reverse.
VIEWS: tuple[tuple[str, str], ...] = (
    ("equity_statement_annual", EQUITY_STATEMENT_ANNUAL),
    ("equity_statement_matrix", EQUITY_STATEMENT_MATRIX),
)


def upgrade() -> None:
    for _name, sql in VIEWS:
        op.execute(sql)


def downgrade() -> None:
    for name, _sql in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
