"""Map the XBRL tags SEC filers used before 2018, and other common fallbacks.

The metric and statement views only knew the tags of the 2018+ us-gaap
taxonomy. That taxonomy retired the revenue and cost tags almost every filer
used until then (SalesRevenueNet, SalesRevenueServicesNet,
SalesRevenueGoodsNet, CostOfGoodsSold, CostOfServices), so a company's
revenue, gross margin and every TTM figure built on them began with FY2016:
the oldest year its 2018 filings restate under the new tags. Filers that tag
net income as ProfitLoss lost it entirely (ProfitLoss was mapped but only
stored for ifrs-full), and so did filers whose cost of revenue is tagged
excluding D&A (e.g. CostOfServicesExcludingDepreciationDepletionAndAmortization)
once they switched to it.

Each fallback joins its metric family (fundamentals_grouped) and statement
line (equity_statement_annual) after the tags already there, so a period
that has a current tag keeps exactly the value it had; only periods with no
current tag gain one. The SEC ingest stores the new tags from its next run
(it re-reads each company's full companyfacts), which backfills the history.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-05

"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (family, tag, priority): priorities continue after 0008's for each family.
LEGACY_FAMILIES = """
    ('revenue', 'SalesRevenueNet', 4),
    ('revenue', 'RevenueFromContractWithCustomerIncludingAssessedTax', 5),
    ('revenue', 'SalesRevenueServicesNet', 6),
    ('revenue', 'SalesRevenueGoodsNet', 7),
    ('cogs', 'CostOfGoodsSold', 4),
    ('cogs', 'CostOfServices', 5),
    ('cogs', 'CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization', 6),
    ('cogs', 'CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization', 7),
    ('cogs', 'CostOfServicesExcludingDepreciationDepletionAndAmortization', 8),
    ('net_income', 'NetIncomeLossAvailableToCommonStockholdersBasic', 4),
    ('eps', 'EarningsPerShareBasicAndDiluted', 3),
    ('shares_diluted', 'WeightedAverageNumberOfShareOutstandingBasicAndDiluted', 4)
"""

# Same lines as 0007's STATEMENT_MAPPING, after the tags each line has.
LEGACY_MAPPING = """
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 4, 'SalesRevenueNet'),
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 5,
     'RevenueFromContractWithCustomerIncludingAssessedTax'),
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 6, 'SalesRevenueServicesNet'),
    ('income', 1, 'Revenue & gross profit', 1, 'Revenue', 7, 'SalesRevenueGoodsNet'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 4, 'CostOfGoodsSold'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 5, 'CostOfServices'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 6,
     'CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 7,
     'CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization'),
    ('income', 1, 'Revenue & gross profit', 2, 'Cost of revenue', 8,
     'CostOfServicesExcludingDepreciationDepletionAndAmortization'),
    ('income', 4, 'Taxes & net income', 3, 'Net income', 4,
     'NetIncomeLossAvailableToCommonStockholdersBasic'),
    ('income', 5, 'Per share', 1, 'EPS (basic)', 3, 'EarningsPerShareBasicAndDiluted'),
    ('income', 5, 'Per share', 2, 'EPS (diluted)', 3, 'EarningsPerShareBasicAndDiluted'),
    ('income', 5, 'Per share', 3, 'Weighted avg shares (basic)', 3,
     'WeightedAverageNumberOfShareOutstandingBasicAndDiluted'),
    ('income', 5, 'Per share', 4, 'Weighted avg shares (diluted)', 3,
     'WeightedAverageNumberOfShareOutstandingBasicAndDiluted')"""


def _load(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def families_0008() -> str:
    rev0008 = _load("0008_valuation_metrics.py", "fintracker_migration_0008")
    return str(rev0008._BASE_FAMILIES).rstrip() + "," + str(rev0008._NEW_FAMILIES).rstrip(",\n")


def fundamentals_grouped_sql(families: str) -> str:
    rev0008 = _load("0008_valuation_metrics.py", "fintracker_migration_0008")
    return str(rev0008._fundamentals_grouped(families))


def statement_annual_sql(extra_mapping: str = "") -> str:
    """0007's equity_statement_annual (with currency), optionally with more mapping rows."""
    rev0007 = _load("0007_currency_views.py", "fintracker_migration_0007")
    sql = str(rev0007._annual_view(with_currency=True))
    assert sql.count("CREATE VIEW equity_statement_annual AS") == 1
    sql = sql.replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)
    if extra_mapping:
        mapping = str(rev0007.STATEMENT_MAPPING)
        assert sql.count(mapping) == 1, "0007's statement mapping text changed"
        sql = sql.replace(mapping, mapping.rstrip() + "," + extra_mapping + "\n")
    return sql


def upgrade() -> None:
    op.execute(fundamentals_grouped_sql(families_0008() + "," + LEGACY_FAMILIES.rstrip()))
    op.execute(statement_annual_sql(LEGACY_MAPPING))


def downgrade() -> None:
    op.execute(statement_annual_sql())
    op.execute(fundamentals_grouped_sql(families_0008()))
