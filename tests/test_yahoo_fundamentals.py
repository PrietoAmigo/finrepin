"""Unit tests for the pure Yahoo statement mapping — no network, no database."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from fintracker.ingest.yahoo_fundamentals import (
    BALANCE_LINES,
    CASHFLOW_LINES,
    INCOME_LINES,
    facts_from_statement,
)

FY25 = pd.Timestamp("2025-12-31")
FY24 = pd.Timestamp("2024-12-31")


def _frame(rows: dict[str, list[float | None]], columns: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index", columns=columns)


def _by_tag(facts: list[dict]) -> dict[str, dict]:
    return {f["tag"]: f for f in facts}


class TestIncomeStatement:
    def test_maps_labels_to_canonical_tags(self) -> None:
        frame = _frame(
            {"Total Revenue": [1000.0, 900.0], "Net Income": [100.0, 90.0]}, [FY25, FY24]
        )
        facts = facts_from_statement(frame, INCOME_LINES, "CAD", instant=False)
        by_key = {(f["tag"], f["period_end"]): f["value"] for f in facts}
        assert by_key == {
            ("Revenues", dt.date(2025, 12, 31)): 1000.0,
            ("Revenues", dt.date(2024, 12, 31)): 900.0,
            ("NetIncomeLoss", dt.date(2025, 12, 31)): 100.0,
            ("NetIncomeLoss", dt.date(2024, 12, 31)): 90.0,
        }
        assert all(f["taxonomy"] == "yahoo" for f in facts)

    def test_annual_flow_period_lands_in_the_annual_window(self) -> None:
        frame = _frame({"Total Revenue": [1000.0]}, [FY25])
        (fact,) = facts_from_statement(frame, INCOME_LINES, "CAD", instant=False)
        assert fact["period_end"] == dt.date(2025, 12, 31)
        assert 330 <= (fact["period_end"] - fact["period_start"]).days <= 400
        assert fact["fiscal_year"] == 2025
        assert fact["fiscal_period"] == "FY"

    def test_quarterly_flow_period_lands_in_the_quarterly_window(self) -> None:
        frame = _frame({"Total Revenue": [250.0]}, [pd.Timestamp("2026-03-31")])
        (fact,) = facts_from_statement(frame, INCOME_LINES, "CAD", instant=False, quarterly=True)
        assert 60 <= (fact["period_end"] - fact["period_start"]).days <= 120
        assert fact["fiscal_period"] == "Q"

    def test_units_for_money_per_share_and_share_counts(self) -> None:
        frame = _frame(
            {
                "Total Revenue": [1000.0],
                "Diluted EPS": [4.2],
                "Diluted Average Shares": [21_000_000.0],
            },
            [FY25],
        )
        facts = _by_tag(facts_from_statement(frame, INCOME_LINES, "EUR", instant=False))
        assert facts["Revenues"]["unit"] == "EUR"
        assert facts["EarningsPerShareDiluted"]["unit"] == "EUR/shares"
        assert facts["WeightedAverageNumberOfDilutedSharesOutstanding"]["unit"] == "shares"

    def test_nan_cells_are_skipped(self) -> None:
        frame = _frame({"Total Revenue": [1000.0, None]}, [FY25, FY24])
        facts = facts_from_statement(frame, INCOME_LINES, "CAD", instant=False)
        assert [f["period_end"] for f in facts] == [dt.date(2025, 12, 31)]

    def test_unmapped_labels_are_ignored(self) -> None:
        frame = _frame({"EBITDA": [500.0], "Tax Effect Of Unusual Items": [1.0]}, [FY25])
        assert facts_from_statement(frame, INCOME_LINES, "CAD", instant=False) == []

    def test_empty_frame_returns_no_facts(self) -> None:
        assert facts_from_statement(pd.DataFrame(), INCOME_LINES, "CAD", instant=False) == []


class TestBalanceSheet:
    def test_instant_facts_use_end_as_start(self) -> None:
        frame = _frame({"Total Assets": [5000.0]}, [FY25])
        (fact,) = facts_from_statement(frame, BALANCE_LINES, "CAD", instant=True)
        assert fact["tag"] == "Assets"
        assert fact["period_start"] == fact["period_end"] == dt.date(2025, 12, 31)

    def test_first_label_wins_when_two_map_to_one_tag(self) -> None:
        # Both "Accounts Receivable" and "Receivables" map to the same tag.
        frame = _frame({"Accounts Receivable": [300.0], "Receivables": [350.0]}, [FY25])
        (fact,) = facts_from_statement(frame, BALANCE_LINES, "CAD", instant=True)
        assert fact["value"] == 300.0


class TestCashFlowSigns:
    def test_outflows_flip_to_the_positive_xbrl_convention(self) -> None:
        frame = _frame(
            {
                "Operating Cash Flow": [800.0],
                "Capital Expenditure": [-120.0],
                "Cash Dividends Paid": [-60.0],
                "Repurchase Of Capital Stock": [-40.0],
            },
            [FY25],
        )
        facts = _by_tag(facts_from_statement(frame, CASHFLOW_LINES, "CAD", instant=False))
        assert facts["NetCashProvidedByUsedInOperatingActivities"]["value"] == 800.0
        assert facts["PaymentsToAcquirePropertyPlantAndEquipment"]["value"] == 120.0
        assert facts["PaymentsOfDividends"]["value"] == 60.0
        assert facts["PaymentsForRepurchaseOfCommonStock"]["value"] == 40.0

    def test_working_capital_changes_flip_to_increase_decrease_convention(self) -> None:
        # Receivables grew by 50 (a cash drag at Yahoo, a positive
        # IncreaseDecreaseInAccountsReceivable under XBRL).
        frame = _frame({"Change In Receivables": [-50.0]}, [FY25])
        (fact,) = facts_from_statement(frame, CASHFLOW_LINES, "CAD", instant=False)
        assert fact["tag"] == "IncreaseDecreaseInAccountsReceivable"
        assert fact["value"] == 50.0


class TestTagsStayInSyncWithViews:
    def test_every_mapped_tag_is_known_to_the_statement_views(self) -> None:
        # Yahoo facts flow through the same views as SEC facts, so every
        # canonical tag must appear in the migration 0006 line mapping.
        sql = (
            Path(__file__).parent.parent
            / "migrations"
            / "versions"
            / "0006_full_statement_views.py"
        ).read_text()
        for lines in (INCOME_LINES, BALANCE_LINES, CASHFLOW_LINES):
            for _label, tag, _sign in lines:
                assert f"'{tag}'" in sql, f"{tag} is not mapped by the statement views"
