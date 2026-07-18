"""Full sectioned statement views for the Financial Statements dashboard.

Replaces the 0005 statement views with a much fuller line-item mapping,
clustered into the sections a filed statement uses (current vs non-current
assets/liabilities, operating/investing/financing activities, ...):

- equity_statement_annual — gains section / section_ord columns; line ord
                            becomes section_ord * 100 + position-in-section.
- equity_statement_matrix — additionally emits one uppercase header row per
                            section that has data, so the dashboard tables
                            read like the as-filed statements.

The extraction rules are unchanged from 0005: flow items take the
annual-duration fact, balance-sheet items take the instant fact at a
fiscal-year end, and ties resolve by tag priority, then USD-unit
preference, then latest filing.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-18

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Maps raw XBRL tags (us-gaap and ifrs-full) to sectioned statement line
# items. `priority` breaks ties when a filer reports several tags for the
# same line and period. Keep in sync with CURATED_TAGS in
# fintracker.ingest.fundamentals.
EQUITY_STATEMENT_ANNUAL = """
CREATE VIEW equity_statement_annual AS
WITH mapping(statement, section_ord, section, item_ord, line, priority, tag) AS (VALUES
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
),
fy_ends AS (
    SELECT DISTINCT instrument_id, period_end
    FROM fundamentals
    WHERE (period_end - period_start) BETWEEN 330 AND 400
),
mapped AS (
    SELECT f.instrument_id, m.statement, m.section_ord, m.section,
           (m.section_ord * 100 + m.item_ord) AS ord, m.line, m.priority,
           f.period_end, f.unit, f.filed_at,
           (f.period_end - f.period_start) AS period_days,
           f.value::float8 AS value
    FROM fundamentals f
    JOIN mapping m ON m.tag = f.tag
)
SELECT DISTINCT ON (instrument_id, statement, line, period_end)
       instrument_id, statement, section_ord, section, ord, line, period_end,
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

# Dense line-item x fiscal-year grid plus one uppercase header row per section
# with data. Grafana's grouping-to-matrix orders rows and columns by first
# appearance, so the grid must be complete (value NULL where the filer didn't
# tag the line) for the layout to stay stable.
EQUITY_STATEMENT_MATRIX = """
CREATE VIEW equity_statement_matrix AS
WITH years AS (
    SELECT DISTINCT instrument_id, statement, fiscal_year
    FROM equity_statement_annual
),
lines AS (
    SELECT DISTINCT instrument_id, statement, section_ord, section, ord, line
    FROM equity_statement_annual
),
headers AS (
    SELECT DISTINCT instrument_id, statement,
           (section_ord * 100) AS ord, upper(section) AS line
    FROM lines
),
grid AS (
    SELECT instrument_id, statement, ord, line FROM lines
    UNION ALL
    SELECT instrument_id, statement, ord, line FROM headers
)
SELECT g.instrument_id, g.statement, g.ord, g.line, y.fiscal_year, a.value
FROM grid g
JOIN years y
  ON y.instrument_id = g.instrument_id
 AND y.statement = g.statement
LEFT JOIN equity_statement_annual a
  ON a.instrument_id = g.instrument_id
 AND a.statement = g.statement
 AND a.line = g.line
 AND a.fiscal_year = y.fiscal_year
"""

# The 0005 definitions, restored verbatim on downgrade.
OLD_EQUITY_STATEMENT_ANNUAL = """
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

OLD_EQUITY_STATEMENT_MATRIX = """
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


def upgrade() -> None:
    op.execute("DROP VIEW equity_statement_matrix")
    op.execute("DROP VIEW equity_statement_annual")
    op.execute(EQUITY_STATEMENT_ANNUAL)
    op.execute(EQUITY_STATEMENT_MATRIX)


def downgrade() -> None:
    op.execute("DROP VIEW equity_statement_matrix")
    op.execute("DROP VIEW equity_statement_annual")
    op.execute(OLD_EQUITY_STATEMENT_ANNUAL)
    op.execute(OLD_EQUITY_STATEMENT_MATRIX)
