"""Currency switching: FX rate views + currency-aware series/statement views.

Backs the display-currency selector on the Grafana dashboards:

- fx_usd_daily          — USD rate per currency per calendar day, gap-filled
                          (weekends/holidays carry the last close forward),
                          built from the '<CCY>/USD' forex instruments that
                          `ensure_fx_instruments` registers; USD itself is
                          included at 1.0 so joins never special-case it.
                          Converting X→Y multiplies by usd_rate(X)/usd_rate(Y).
- instrument_currencies — per instrument: the listing (price) currency and
                          the reporting currency (the most common currency
                          unit among its fundamentals facts — CSU.TO trades
                          in CAD but reports in USD).
- equity_metric_series  — gains a trailing `currency` column: reporting
                          currency for revenue/debt, price currency for mcap,
                          NULL for ratios, margins, and share counts.
- equity_statement_annual / equity_statement_matrix — gain a `currency`
  column (NULL for share-count lines); the matrix also exposes `period_end`
  so dashboards can convert each cell at its fiscal-year-end FX rate.

The statement-view line mapping is unchanged from 0006; only the output
columns differ.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-18

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INSTRUMENT_CURRENCIES = """
CREATE VIEW instrument_currencies AS
SELECT i.id AS instrument_id,
       i.currency AS price_currency,
       (SELECT split_part(f.unit, '/', 1)
        FROM fundamentals f
        WHERE f.instrument_id = i.id
          AND f.unit ~ '^[A-Z]{3}(/shares)?$'
        GROUP BY 1
        ORDER BY count(*) DESC
        LIMIT 1) AS reporting_currency
FROM instruments i
"""

# Forward fill via the count-of-non-nulls grouping trick: every calendar day
# joins the last observed close on or before it.
FX_USD_DAILY = """
CREATE VIEW fx_usd_daily AS
WITH pairs AS (
    SELECT id AS instrument_id, split_part(symbol, '/', 2) AS quote,
           split_part(symbol, '/', 1) AS currency
    FROM instruments
    WHERE kind = 'forex' AND symbol LIKE '%/USD'
),
raw AS (
    SELECT pa.currency, pr.date, pr.close::float8 AS usd_rate
    FROM pairs pa
    JOIN prices pr ON pr.instrument_id = pa.instrument_id
),
cal AS (
    SELECT currency,
           generate_series(min(date),
                           GREATEST(max(date), CURRENT_DATE),
                           interval '1 day')::date AS date
    FROM raw
    GROUP BY currency
),
joined AS (
    SELECT c.currency, c.date, r.usd_rate,
           COUNT(r.usd_rate) OVER (PARTITION BY c.currency ORDER BY c.date) AS grp
    FROM cal c
    LEFT JOIN raw r ON r.currency = c.currency AND r.date = c.date
)
SELECT currency, date,
       MAX(usd_rate) OVER (PARTITION BY currency, grp) AS usd_rate
FROM joined
UNION ALL
SELECT 'USD', gs::date, 1.0
FROM generate_series((SELECT min(date) FROM prices),
                     GREATEST((SELECT max(date) FROM prices), CURRENT_DATE),
                     interval '1 day') AS gs
"""

# The 0004 view plus a trailing `currency` column per branch.
EQUITY_METRIC_SERIES = """
CREATE VIEW equity_metric_series AS
WITH eps_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_flows_ttm
    WHERE grp = 'eps'
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
fcf_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_fcf_ttm
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
shares_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM (
        SELECT DISTINCT ON (instrument_id, period_end)
               instrument_id, period_end, value
        FROM fundamentals_grouped
        WHERE grp = 'shares_diluted'
        ORDER BY instrument_id, period_end, period_days ASC
    ) s
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
)
SELECT t.instrument_id, 'revenue' AS metric, 'Revenue (TTM)' AS label,
       t.period_end::timestamptz AS "time", t.value AS value,
       ic.reporting_currency AS currency
FROM equity_flows_ttm t
LEFT JOIN instrument_currencies ic ON ic.instrument_id = t.instrument_id
WHERE t.grp = 'revenue'
UNION ALL
SELECT o.instrument_id, 'op_margin', 'Operating margin (TTM)',
       o.period_end::timestamptz, 100.0 * o.value / NULLIF(r.value, 0),
       NULL::text
FROM equity_flows_ttm o
JOIN equity_flows_ttm r
  ON r.instrument_id = o.instrument_id
 AND r.period_end = o.period_end
 AND r.grp = 'revenue'
WHERE o.grp = 'op_income'
UNION ALL
SELECT d.instrument_id, 'debt', 'Total debt', d.period_end::timestamptz, d.value,
       ic.reporting_currency
FROM equity_debt d
LEFT JOIN instrument_currencies ic ON ic.instrument_id = d.instrument_id
UNION ALL
SELECT p.instrument_id, 'pe', 'P/E (TTM)', p.date::timestamptz,
       CASE WHEN e.value > 0 THEN p.close::float8 / e.value END,
       NULL::text
FROM prices p
JOIN eps_w e
  ON e.instrument_id = p.instrument_id
 AND p.date >= e.period_end
 AND p.date < e.next_end
 AND p.date < (e.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'pfcf', 'P/FCF (TTM)', p.date::timestamptz,
       CASE WHEN f.value > 0 THEN (p.close::float8 * s.value) / f.value END,
       NULL::text
FROM prices p
JOIN fcf_w f
  ON f.instrument_id = p.instrument_id
 AND p.date >= f.period_end
 AND p.date < f.next_end
 AND p.date < (f.period_end + 450)
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'mcap', 'MCap', p.date::timestamptz,
       p.close::float8 * s.value,
       ic.price_currency
FROM prices p
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
LEFT JOIN instrument_currencies ic ON ic.instrument_id = p.instrument_id
UNION ALL
SELECT instrument_id, 'shares', 'Shares outstanding',
       period_end::timestamptz, value, NULL::text
FROM shares_w
"""

# The 0006 statement views with the same line mapping; the annual view is
# rebuilt from the shared mapping below, with or without the currency column.
STATEMENT_MAPPING = """
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 1,
     'RevenueFromContractWithCustomerExcludingAssessedTax'),
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 2, 'Revenues'),
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 3, 'Revenue'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 1, 'CostOfRevenue'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 2, 'CostOfGoodsAndServicesSold'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 3, 'CostOfSales'),
    ('income', 1, 'Revenue & gross profit', 3, 'Gross profit', 1, 'GrossProfit'),

    ('income', 2, 'Operating expenses', 1, 'R&D expense', 1, 'ResearchAndDevelopmentExpense'),
    ('income', 2, 'Operating expenses', 2, 'SG&A expense', 1,
     'SellingGeneralAndAdministrativeExpense'),
    ('income', 2, 'Operating expenses', 3, 'Selling & marketing expense', 1,
     'SellingAndMarketingExpense'),
    ('income', 2, 'Operating expenses', 4, 'G&A expense', 1, 'GeneralAndAdministrativeExpense'),
    ('income', 2, 'Operating expenses', 5, 'Restructuring charges', 1, 'RestructuringCharges'),
    ('income', 2, 'Operating expenses', 6, 'Impairments', 1, 'AssetImpairmentCharges'),
    ('income', 2, 'Operating expenses', 6, 'Impairments', 2, 'GoodwillImpairmentLoss'),
    ('income', 2, 'Operating expenses', 7, 'Total operating expenses', 1, 'OperatingExpenses'),
    ('income', 2, 'Operating expenses', 7, 'Total operating expenses', 2, 'OperatingExpense'),
    ('income', 2, 'Operating expenses', 8, 'Operating income', 1, 'OperatingIncomeLoss'),
    ('income', 2, 'Operating expenses', 8, 'Operating income', 2,
     'ProfitLossFromOperatingActivities'),

    ('income', 3, 'Non-operating', 1, 'Interest income', 1, 'InvestmentIncomeInterest'),
    ('income', 3, 'Non-operating', 1, 'Interest income', 2, 'FinanceIncome'),
    ('income', 3, 'Non-operating', 2, 'Interest expense', 1, 'InterestExpense'),
    ('income', 3, 'Non-operating', 2, 'Interest expense', 2, 'InterestExpenseNonoperating'),
    ('income', 3, 'Non-operating', 2, 'Interest expense', 3, 'FinanceCosts'),
    ('income', 3, 'Non-operating', 3, 'Equity method income', 1,
     'IncomeLossFromEquityMethodInvestments'),
    ('income', 3, 'Non-operating', 3, 'Equity method income', 2,
     'ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod'),
    ('income', 3, 'Non-operating', 4, 'Other income (expense), net', 1,
     'OtherNonoperatingIncomeExpense'),
    ('income', 3, 'Non-operating', 4, 'Other income (expense), net', 2,
     'NonoperatingIncomeExpense'),

    ('income', 4, 'Taxes & net income', 1, 'Pretax income', 1,
     'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'),
    ('income', 4, 'Taxes & net income', 1, 'Pretax income', 2,
     'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments'),
    ('income', 4, 'Taxes & net income', 1, 'Pretax income', 3, 'ProfitLossBeforeTax'),
    ('income', 4, 'Taxes & net income', 2, 'Income tax expense', 1, 'IncomeTaxExpenseBenefit'),
    ('income', 4, 'Taxes & net income', 2, 'Income tax expense', 2,
     'IncomeTaxExpenseContinuingOperations'),
    ('income', 4, 'Taxes & net income', 3, 'Net income', 1, 'NetIncomeLoss'),
    ('income', 4, 'Taxes & net income', 3, 'Net income', 2,
     'ProfitLossAttributableToOwnersOfParent'),
    ('income', 4, 'Taxes & net income', 3, 'Net income', 3, 'ProfitLoss'),
    ('income', 4, 'Taxes & net income', 4, 'Net income to noncontrolling interests', 1,
     'NetIncomeLossAttributableToNoncontrollingInterest'),
    ('income', 4, 'Taxes & net income', 4, 'Net income to noncontrolling interests', 2,
     'ProfitLossAttributableToNoncontrollingInterests'),
    ('income', 4, 'Taxes & net income', 5, 'Comprehensive income', 1,
     'ComprehensiveIncomeNetOfTax'),
    ('income', 4, 'Taxes & net income', 5, 'Comprehensive income', 2, 'ComprehensiveIncome'),

    ('income', 5, 'Per share', 1, 'EPS (basic)', 1, 'EarningsPerShareBasic'),
    ('income', 5, 'Per share', 1, 'EPS (basic)', 2, 'BasicEarningsLossPerShare'),
    ('income', 5, 'Per share', 2, 'EPS (diluted)', 1, 'EarningsPerShareDiluted'),
    ('income', 5, 'Per share', 2, 'EPS (diluted)', 2, 'DilutedEarningsLossPerShare'),
    ('income', 5, 'Per share', 3, 'Weighted avg shares (basic)', 1,
     'WeightedAverageNumberOfSharesOutstandingBasic'),
    ('income', 5, 'Per share', 3, 'Weighted avg shares (basic)', 2, 'WeightedAverageShares'),
    ('income', 5, 'Per share', 4, 'Weighted avg shares (diluted)', 1,
     'WeightedAverageNumberOfDilutedSharesOutstanding'),
    ('income', 5, 'Per share', 4, 'Weighted avg shares (diluted)', 2,
     'AdjustedWeightedAverageShares'),
    ('income', 5, 'Per share', 5, 'Dividends per share', 1,
     'CommonStockDividendsPerShareDeclared'),
    ('income', 5, 'Per share', 5, 'Dividends per share', 2,
     'CommonStockDividendsPerShareCashPaid'),
    ('income', 5, 'Per share', 5, 'Dividends per share', 3,
     'DividendsPaidOrdinarySharesPerShare'),

    ('balance', 1, 'Current assets', 1, 'Cash & equivalents', 1,
     'CashAndCashEquivalentsAtCarryingValue'),
    ('balance', 1, 'Current assets', 1, 'Cash & equivalents', 2, 'CashAndCashEquivalents'),
    ('balance', 1, 'Current assets', 2, 'Short-term investments', 1, 'ShortTermInvestments'),
    ('balance', 1, 'Current assets', 2, 'Short-term investments', 2,
     'MarketableSecuritiesCurrent'),
    ('balance', 1, 'Current assets', 3, 'Receivables', 1, 'AccountsReceivableNetCurrent'),
    ('balance', 1, 'Current assets', 3, 'Receivables', 2, 'TradeAndOtherCurrentReceivables'),
    ('balance', 1, 'Current assets', 4, 'Inventory', 1, 'InventoryNet'),
    ('balance', 1, 'Current assets', 4, 'Inventory', 2, 'Inventories'),
    ('balance', 1, 'Current assets', 5, 'Prepaid expenses', 1,
     'PrepaidExpenseAndOtherAssetsCurrent'),
    ('balance', 1, 'Current assets', 5, 'Prepaid expenses', 2, 'PrepaidExpenseCurrent'),
    ('balance', 1, 'Current assets', 6, 'Other current assets', 1, 'OtherAssetsCurrent'),
    ('balance', 1, 'Current assets', 6, 'Other current assets', 2, 'OtherCurrentAssets'),
    ('balance', 1, 'Current assets', 7, 'Total current assets', 1, 'AssetsCurrent'),
    ('balance', 1, 'Current assets', 7, 'Total current assets', 2, 'CurrentAssets'),

    ('balance', 2, 'Non-current assets', 1, 'Property, plant & equipment', 1,
     'PropertyPlantAndEquipmentNet'),
    ('balance', 2, 'Non-current assets', 1, 'Property, plant & equipment', 2,
     'PropertyPlantAndEquipment'),
    ('balance', 2, 'Non-current assets', 2, 'Operating lease right-of-use assets', 1,
     'OperatingLeaseRightOfUseAsset'),
    ('balance', 2, 'Non-current assets', 2, 'Operating lease right-of-use assets', 2,
     'RightofuseAssets'),
    ('balance', 2, 'Non-current assets', 3, 'Goodwill', 1, 'Goodwill'),
    ('balance', 2, 'Non-current assets', 4, 'Intangible assets', 1,
     'IntangibleAssetsNetExcludingGoodwill'),
    ('balance', 2, 'Non-current assets', 4, 'Intangible assets', 2,
     'IntangibleAssetsOtherThanGoodwill'),
    ('balance', 2, 'Non-current assets', 5, 'Long-term investments', 1, 'LongTermInvestments'),
    ('balance', 2, 'Non-current assets', 5, 'Long-term investments', 2,
     'MarketableSecuritiesNoncurrent'),
    ('balance', 2, 'Non-current assets', 5, 'Long-term investments', 3,
     'OtherNoncurrentFinancialAssets'),
    ('balance', 2, 'Non-current assets', 6, 'Deferred tax assets', 1,
     'DeferredIncomeTaxAssetsNet'),
    ('balance', 2, 'Non-current assets', 6, 'Deferred tax assets', 2,
     'DeferredTaxAssetsNetNoncurrent'),
    ('balance', 2, 'Non-current assets', 6, 'Deferred tax assets', 3, 'DeferredTaxAssets'),
    ('balance', 2, 'Non-current assets', 7, 'Other non-current assets', 1,
     'OtherAssetsNoncurrent'),
    ('balance', 2, 'Non-current assets', 7, 'Other non-current assets', 2,
     'OtherNoncurrentAssets'),
    ('balance', 2, 'Non-current assets', 8, 'Total non-current assets', 1, 'AssetsNoncurrent'),
    ('balance', 2, 'Non-current assets', 8, 'Total non-current assets', 2, 'NoncurrentAssets'),
    ('balance', 2, 'Non-current assets', 9, 'Total assets', 1, 'Assets'),

    ('balance', 3, 'Current liabilities', 1, 'Accounts payable', 1, 'AccountsPayableCurrent'),
    ('balance', 3, 'Current liabilities', 1, 'Accounts payable', 2,
     'AccountsPayableAndAccruedLiabilitiesCurrent'),
    ('balance', 3, 'Current liabilities', 1, 'Accounts payable', 3,
     'TradeAndOtherCurrentPayables'),
    ('balance', 3, 'Current liabilities', 2, 'Accrued liabilities', 1,
     'AccruedLiabilitiesCurrent'),
    ('balance', 3, 'Current liabilities', 3, 'Deferred revenue (current)', 1,
     'ContractWithCustomerLiabilityCurrent'),
    ('balance', 3, 'Current liabilities', 3, 'Deferred revenue (current)', 2,
     'DeferredRevenueCurrent'),
    ('balance', 3, 'Current liabilities', 4, 'Short-term debt', 1, 'DebtCurrent'),
    ('balance', 3, 'Current liabilities', 4, 'Short-term debt', 2, 'LongTermDebtCurrent'),
    ('balance', 3, 'Current liabilities', 4, 'Short-term debt', 3, 'ShortTermBorrowings'),
    ('balance', 3, 'Current liabilities', 4, 'Short-term debt', 4,
     'CurrentPortionOfLongtermBorrowings'),
    ('balance', 3, 'Current liabilities', 4, 'Short-term debt', 5, 'ShorttermBorrowings'),
    ('balance', 3, 'Current liabilities', 5, 'Operating lease liabilities (current)', 1,
     'OperatingLeaseLiabilityCurrent'),
    ('balance', 3, 'Current liabilities', 5, 'Operating lease liabilities (current)', 2,
     'CurrentLeaseLiabilities'),
    ('balance', 3, 'Current liabilities', 6, 'Other current liabilities', 1,
     'OtherLiabilitiesCurrent'),
    ('balance', 3, 'Current liabilities', 6, 'Other current liabilities', 2,
     'OtherCurrentLiabilities'),
    ('balance', 3, 'Current liabilities', 7, 'Total current liabilities', 1,
     'LiabilitiesCurrent'),
    ('balance', 3, 'Current liabilities', 7, 'Total current liabilities', 2,
     'CurrentLiabilities'),

    ('balance', 4, 'Non-current liabilities', 1, 'Long-term debt', 1, 'LongTermDebtNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 1, 'Long-term debt', 2, 'LongtermBorrowings'),
    ('balance', 4, 'Non-current liabilities', 1, 'Long-term debt', 3, 'LongTermDebt'),
    ('balance', 4, 'Non-current liabilities', 2, 'Operating lease liabilities (non-current)', 1,
     'OperatingLeaseLiabilityNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 2, 'Operating lease liabilities (non-current)', 2,
     'NoncurrentLeaseLiabilities'),
    ('balance', 4, 'Non-current liabilities', 3, 'Deferred revenue (non-current)', 1,
     'ContractWithCustomerLiabilityNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 3, 'Deferred revenue (non-current)', 2,
     'DeferredRevenueNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 4, 'Deferred tax liabilities', 1,
     'DeferredIncomeTaxLiabilitiesNet'),
    ('balance', 4, 'Non-current liabilities', 4, 'Deferred tax liabilities', 2,
     'DeferredTaxLiabilitiesNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 4, 'Deferred tax liabilities', 3,
     'DeferredTaxLiabilities'),
    ('balance', 4, 'Non-current liabilities', 5, 'Other non-current liabilities', 1,
     'OtherLiabilitiesNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 5, 'Other non-current liabilities', 2,
     'OtherNoncurrentLiabilities'),
    ('balance', 4, 'Non-current liabilities', 6, 'Total non-current liabilities', 1,
     'LiabilitiesNoncurrent'),
    ('balance', 4, 'Non-current liabilities', 6, 'Total non-current liabilities', 2,
     'NoncurrentLiabilities'),
    ('balance', 4, 'Non-current liabilities', 7, 'Total liabilities', 1, 'Liabilities'),

    ('balance', 5, 'Equity', 1, 'Common stock & paid-in capital', 1,
     'CommonStocksIncludingAdditionalPaidInCapital'),
    ('balance', 5, 'Equity', 1, 'Common stock & paid-in capital', 2, 'AdditionalPaidInCapital'),
    ('balance', 5, 'Equity', 1, 'Common stock & paid-in capital', 3, 'CommonStockValue'),
    ('balance', 5, 'Equity', 1, 'Common stock & paid-in capital', 4, 'IssuedCapital'),
    ('balance', 5, 'Equity', 2, 'Treasury stock', 1, 'TreasuryStockCommonValue'),
    ('balance', 5, 'Equity', 2, 'Treasury stock', 2, 'TreasuryStockValue'),
    ('balance', 5, 'Equity', 2, 'Treasury stock', 3, 'TreasuryShares'),
    ('balance', 5, 'Equity', 3, 'Retained earnings', 1, 'RetainedEarningsAccumulatedDeficit'),
    ('balance', 5, 'Equity', 3, 'Retained earnings', 2, 'RetainedEarnings'),
    ('balance', 5, 'Equity', 4, 'Accumulated other comprehensive income', 1,
     'AccumulatedOtherComprehensiveIncomeLossNetOfTax'),
    ('balance', 5, 'Equity', 4, 'Accumulated other comprehensive income', 2,
     'AccumulatedOtherComprehensiveIncome'),
    ('balance', 5, 'Equity', 5, 'Noncontrolling interests', 1, 'MinorityInterest'),
    ('balance', 5, 'Equity', 5, 'Noncontrolling interests', 2, 'NoncontrollingInterests'),
    ('balance', 5, 'Equity', 6, 'Total equity', 1, 'StockholdersEquity'),
    ('balance', 5, 'Equity', 6, 'Total equity', 2, 'EquityAttributableToOwnersOfParent'),
    ('balance', 5, 'Equity', 6, 'Total equity', 3,
     'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'),
    ('balance', 5, 'Equity', 6, 'Total equity', 4, 'Equity'),

    ('cashflow', 1, 'Operating activities', 1, 'Net income', 1, 'NetIncomeLoss'),
    ('cashflow', 1, 'Operating activities', 1, 'Net income', 2, 'ProfitLoss'),
    ('cashflow', 1, 'Operating activities', 2, 'Depreciation & amortization', 1,
     'DepreciationDepletionAndAmortization'),
    ('cashflow', 1, 'Operating activities', 2, 'Depreciation & amortization', 2,
     'DepreciationAndAmortization'),
    ('cashflow', 1, 'Operating activities', 2, 'Depreciation & amortization', 3,
     'DepreciationAndAmortisationExpense'),
    ('cashflow', 1, 'Operating activities', 3, 'Share-based compensation', 1,
     'ShareBasedCompensation'),
    ('cashflow', 1, 'Operating activities', 4, 'Deferred income taxes', 1,
     'DeferredIncomeTaxExpenseBenefit'),
    ('cashflow', 1, 'Operating activities', 5, 'Change in receivables', 1,
     'IncreaseDecreaseInAccountsReceivable'),
    ('cashflow', 1, 'Operating activities', 6, 'Change in inventory', 1,
     'IncreaseDecreaseInInventories'),
    ('cashflow', 1, 'Operating activities', 7, 'Change in payables', 1,
     'IncreaseDecreaseInAccountsPayable'),
    ('cashflow', 1, 'Operating activities', 7, 'Change in payables', 2,
     'IncreaseDecreaseInAccountsPayableAndAccruedLiabilities'),
    ('cashflow', 1, 'Operating activities', 8, 'Change in deferred revenue', 1,
     'IncreaseDecreaseInContractWithCustomerLiability'),
    ('cashflow', 1, 'Operating activities', 8, 'Change in deferred revenue', 2,
     'IncreaseDecreaseInDeferredRevenue'),
    ('cashflow', 1, 'Operating activities', 9, 'Operating cash flow', 1,
     'NetCashProvidedByUsedInOperatingActivities'),
    ('cashflow', 1, 'Operating activities', 9, 'Operating cash flow', 2,
     'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'),
    ('cashflow', 1, 'Operating activities', 9, 'Operating cash flow', 3,
     'CashFlowsFromUsedInOperatingActivities'),

    ('cashflow', 2, 'Investing activities', 1, 'Capital expenditure', 1,
     'PaymentsToAcquirePropertyPlantAndEquipment'),
    ('cashflow', 2, 'Investing activities', 1, 'Capital expenditure', 2,
     'PaymentsToAcquireProductiveAssets'),
    ('cashflow', 2, 'Investing activities', 1, 'Capital expenditure', 3,
     'PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'),
    ('cashflow', 2, 'Investing activities', 2, 'Acquisitions (net of cash)', 1,
     'PaymentsToAcquireBusinessesNetOfCashAcquired'),
    ('cashflow', 2, 'Investing activities', 2, 'Acquisitions (net of cash)', 2,
     'CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities'),
    ('cashflow', 2, 'Investing activities', 3, 'Purchases of investments', 1,
     'PaymentsToAcquireInvestments'),
    ('cashflow', 2, 'Investing activities', 3, 'Purchases of investments', 2,
     'PaymentsToAcquireMarketableSecurities'),
    ('cashflow', 2, 'Investing activities', 3, 'Purchases of investments', 3,
     'PaymentsToAcquireAvailableForSaleSecuritiesDebt'),
    ('cashflow', 2, 'Investing activities', 4, 'Sales & maturities of investments', 1,
     'ProceedsFromSaleMaturityAndCollectionsOfInvestments'),
    ('cashflow', 2, 'Investing activities', 4, 'Sales & maturities of investments', 2,
     'ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities'),
    ('cashflow', 2, 'Investing activities', 4, 'Sales & maturities of investments', 3,
     'ProceedsFromSaleAndMaturityOfMarketableSecurities'),
    ('cashflow', 2, 'Investing activities', 5, 'Investing cash flow', 1,
     'NetCashProvidedByUsedInInvestingActivities'),
    ('cashflow', 2, 'Investing activities', 5, 'Investing cash flow', 2,
     'NetCashProvidedByUsedInInvestingActivitiesContinuingOperations'),
    ('cashflow', 2, 'Investing activities', 5, 'Investing cash flow', 3,
     'CashFlowsFromUsedInInvestingActivities'),

    ('cashflow', 3, 'Financing activities', 1, 'Debt issued', 1,
     'ProceedsFromIssuanceOfLongTermDebt'),
    ('cashflow', 3, 'Financing activities', 1, 'Debt issued', 2,
     'ProceedsFromBorrowingsClassifiedAsFinancingActivities'),
    ('cashflow', 3, 'Financing activities', 2, 'Debt repaid', 1, 'RepaymentsOfLongTermDebt'),
    ('cashflow', 3, 'Financing activities', 2, 'Debt repaid', 2, 'RepaymentsOfDebt'),
    ('cashflow', 3, 'Financing activities', 2, 'Debt repaid', 3,
     'RepaymentsOfBorrowingsClassifiedAsFinancingActivities'),
    ('cashflow', 3, 'Financing activities', 3, 'Stock issued', 1,
     'ProceedsFromIssuanceOfCommonStock'),
    ('cashflow', 3, 'Financing activities', 4, 'Share buybacks', 1,
     'PaymentsForRepurchaseOfCommonStock'),
    ('cashflow', 3, 'Financing activities', 5, 'Dividends paid', 1, 'PaymentsOfDividends'),
    ('cashflow', 3, 'Financing activities', 5, 'Dividends paid', 2,
     'PaymentsOfDividendsCommonStock'),
    ('cashflow', 3, 'Financing activities', 5, 'Dividends paid', 3,
     'DividendsPaidClassifiedAsFinancingActivities'),
    ('cashflow', 3, 'Financing activities', 6, 'Financing cash flow', 1,
     'NetCashProvidedByUsedInFinancingActivities'),
    ('cashflow', 3, 'Financing activities', 6, 'Financing cash flow', 2,
     'NetCashProvidedByUsedInFinancingActivitiesContinuingOperations'),
    ('cashflow', 3, 'Financing activities', 6, 'Financing cash flow', 3,
     'CashFlowsFromUsedInFinancingActivities'),

    ('cashflow', 4, 'Net change', 1, 'Effect of exchange rates', 1,
     'EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'),
    ('cashflow', 4, 'Net change', 1, 'Effect of exchange rates', 2,
     'EffectOfExchangeRateOnCashAndCashEquivalents'),
    ('cashflow', 4, 'Net change', 1, 'Effect of exchange rates', 3,
     'EffectOfExchangeRateChangesOnCashAndCashEquivalents'),
    ('cashflow', 4, 'Net change', 2, 'Net change in cash', 1,
     'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect'),
    ('cashflow', 4, 'Net change', 2, 'Net change in cash', 2,
     'CashAndCashEquivalentsPeriodIncreaseDecrease'),
    ('cashflow', 4, 'Net change', 2, 'Net change in cash', 3,
     'IncreaseDecreaseInCashAndCashEquivalents')
"""

_CURRENCY_COLUMN = (
    ",\n       CASE WHEN unit ~ '^[A-Z]{3}(/shares)?$'"
    " THEN split_part(unit, '/', 1) END AS currency"
)


def _annual_view(with_currency: bool) -> str:
    return (
        "CREATE VIEW equity_statement_annual AS\n"
        "WITH mapping(statement, section_ord, section, item_ord, line, priority, tag)"
        " AS (VALUES" + STATEMENT_MAPPING + "),\n"
        "fy_ends AS (\n"
        "    SELECT DISTINCT instrument_id, period_end\n"
        "    FROM fundamentals\n"
        "    WHERE (period_end - period_start) BETWEEN 330 AND 400\n"
        "),\n"
        "mapped AS (\n"
        "    SELECT f.instrument_id, m.statement, m.section_ord, m.section,\n"
        "           (m.section_ord * 100 + m.item_ord) AS ord, m.line, m.priority,\n"
        "           f.period_end, f.unit, f.filed_at,\n"
        "           (f.period_end - f.period_start) AS period_days,\n"
        "           f.value::float8 AS value\n"
        "    FROM fundamentals f\n"
        "    JOIN mapping m ON m.tag = f.tag\n"
        ")\n"
        "SELECT DISTINCT ON (instrument_id, statement, line, period_end)\n"
        "       instrument_id, statement, section_ord, section, ord, line, period_end,\n"
        "       EXTRACT(YEAR FROM period_end)::int AS fiscal_year,\n"
        "       value" + (_CURRENCY_COLUMN if with_currency else "") + "\n"
        "FROM mapped\n"
        "WHERE (statement IN ('income', 'cashflow') AND period_days BETWEEN 330 AND 400)\n"
        "   OR (statement = 'balance' AND period_days = 0\n"
        "       AND EXISTS (\n"
        "           SELECT 1 FROM fy_ends e\n"
        "           WHERE e.instrument_id = mapped.instrument_id\n"
        "             AND e.period_end = mapped.period_end\n"
        "       ))\n"
        "ORDER BY instrument_id, statement, line, period_end,\n"
        "         priority,\n"
        "         CASE WHEN unit IN ('USD', 'USD/shares', 'shares') THEN 0 ELSE 1 END,\n"
        "         filed_at DESC NULLS LAST"
    )


def _matrix_view(with_currency: bool) -> str:
    extra = ", a.currency, a.period_end" if with_currency else ""
    return (
        "CREATE VIEW equity_statement_matrix AS\n"
        "WITH years AS (\n"
        "    SELECT DISTINCT instrument_id, statement, fiscal_year\n"
        "    FROM equity_statement_annual\n"
        "),\n"
        "lines AS (\n"
        "    SELECT DISTINCT instrument_id, statement, section_ord, section, ord, line\n"
        "    FROM equity_statement_annual\n"
        "),\n"
        "headers AS (\n"
        "    SELECT DISTINCT instrument_id, statement,\n"
        "           (section_ord * 100) AS ord, upper(section) AS line\n"
        "    FROM lines\n"
        "),\n"
        "grid AS (\n"
        "    SELECT instrument_id, statement, ord, line FROM lines\n"
        "    UNION ALL\n"
        "    SELECT instrument_id, statement, ord, line FROM headers\n"
        ")\n"
        "SELECT g.instrument_id, g.statement, g.ord, g.line, y.fiscal_year, a.value" + extra + "\n"
        "FROM grid g\n"
        "JOIN years y\n"
        "  ON y.instrument_id = g.instrument_id\n"
        " AND y.statement = g.statement\n"
        "LEFT JOIN equity_statement_annual a\n"
        "  ON a.instrument_id = g.instrument_id\n"
        " AND a.statement = g.statement\n"
        " AND a.line = g.line\n"
        " AND a.fiscal_year = y.fiscal_year"
    )


# The 0004 metric-series view, restored verbatim on downgrade.
OLD_EQUITY_METRIC_SERIES = """
CREATE VIEW equity_metric_series AS
WITH eps_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_flows_ttm
    WHERE grp = 'eps'
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
fcf_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM equity_fcf_ttm
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
),
shares_w AS (
    SELECT instrument_id, period_end,
           COALESCE(LEAD(period_end) OVER w, DATE '9999-12-31') AS next_end,
           value
    FROM (
        SELECT DISTINCT ON (instrument_id, period_end)
               instrument_id, period_end, value
        FROM fundamentals_grouped
        WHERE grp = 'shares_diluted'
        ORDER BY instrument_id, period_end, period_days ASC
    ) s
    WINDOW w AS (PARTITION BY instrument_id ORDER BY period_end)
)
SELECT instrument_id, 'revenue' AS metric, 'Revenue (TTM)' AS label,
       period_end::timestamptz AS "time", value
FROM equity_flows_ttm
WHERE grp = 'revenue'
UNION ALL
SELECT o.instrument_id, 'op_margin', 'Operating margin (TTM)',
       o.period_end::timestamptz, 100.0 * o.value / NULLIF(r.value, 0)
FROM equity_flows_ttm o
JOIN equity_flows_ttm r
  ON r.instrument_id = o.instrument_id
 AND r.period_end = o.period_end
 AND r.grp = 'revenue'
WHERE o.grp = 'op_income'
UNION ALL
SELECT instrument_id, 'debt', 'Total debt', period_end::timestamptz, value
FROM equity_debt
UNION ALL
SELECT p.instrument_id, 'pe', 'P/E (TTM)', p.date::timestamptz,
       CASE WHEN e.value > 0 THEN p.close::float8 / e.value END
FROM prices p
JOIN eps_w e
  ON e.instrument_id = p.instrument_id
 AND p.date >= e.period_end
 AND p.date < e.next_end
 AND p.date < (e.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'pfcf', 'P/FCF (TTM)', p.date::timestamptz,
       CASE WHEN f.value > 0 THEN (p.close::float8 * s.value) / f.value END
FROM prices p
JOIN fcf_w f
  ON f.instrument_id = p.instrument_id
 AND p.date >= f.period_end
 AND p.date < f.next_end
 AND p.date < (f.period_end + 450)
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
UNION ALL
SELECT p.instrument_id, 'mcap', 'MCap', p.date::timestamptz,
       p.close::float8 * s.value
FROM prices p
JOIN shares_w s
  ON s.instrument_id = p.instrument_id
 AND p.date >= s.period_end
 AND p.date < s.next_end
 AND p.date < (s.period_end + 450)
UNION ALL
SELECT instrument_id, 'shares', 'Shares outstanding',
       period_end::timestamptz, value
FROM shares_w
"""


def upgrade() -> None:
    op.execute(INSTRUMENT_CURRENCIES)
    op.execute(FX_USD_DAILY)
    op.execute("DROP VIEW equity_metric_series")
    op.execute(EQUITY_METRIC_SERIES)
    op.execute("DROP VIEW equity_statement_matrix")
    op.execute("DROP VIEW equity_statement_annual")
    op.execute(_annual_view(with_currency=True))
    op.execute(_matrix_view(with_currency=True))


def downgrade() -> None:
    op.execute("DROP VIEW equity_statement_matrix")
    op.execute("DROP VIEW equity_statement_annual")
    op.execute(_annual_view(with_currency=False))
    op.execute(_matrix_view(with_currency=False))
    op.execute("DROP VIEW equity_metric_series")
    op.execute(OLD_EQUITY_METRIC_SERIES)
    op.execute("DROP VIEW fx_usd_daily")
    op.execute("DROP VIEW instrument_currencies")
