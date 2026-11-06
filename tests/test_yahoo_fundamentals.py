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
    interim_period_days,
    superseded_fact_ids,
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


def _ts(*days: str) -> list[pd.Timestamp]:
    return [pd.Timestamp(d) for d in days]


class TestInterimPeriods:
    """Yahoo's "quarterly" statements hold half-years for companies that report
    half-yearly; stored as quarters they would read as Q2 and Q4."""

    def test_quarter_spacing_stays_quarterly(self) -> None:
        cols = _ts("2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31")
        frame = _frame({"Total Revenue": [4.0, 3.0, 2.0, 1.0]}, cols)
        facts = facts_from_statement(frame, INCOME_LINES, "EUR", instant=False, quarterly=True)
        assert {(f["period_end"] - f["period_start"]).days for f in facts} == {91}
        assert {f["fiscal_period"] for f in facts} == {"Q"}

    def test_half_year_spacing_is_stored_as_half_years(self) -> None:
        cols = _ts("2026-06-30", "2025-12-31", "2025-06-30")
        frame = _frame({"Total Revenue": [3.0, 2.0, 1.0]}, cols)
        facts = facts_from_statement(frame, INCOME_LINES, "EUR", instant=False, quarterly=True)
        assert {(f["period_end"] - f["period_start"]).days for f in facts} == {182}
        # Outside both the quarterly (60..120) and annual (330..400) windows.
        assert {f["fiscal_period"] for f in facts} == {"H"}

    def test_each_line_is_measured_on_its_own_values(self) -> None:
        # Quarterly revenue, half-yearly earnings: NaN quarters don't count.
        cols = _ts("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")
        frame = _frame(
            {
                "Total Revenue": [5.0, 4.0, 3.0, 2.0, 1.0],
                "Net Income": [2.0, None, 1.5, None, 1.0],
            },
            cols,
        )
        facts = facts_from_statement(frame, INCOME_LINES, "EUR", instant=False, quarterly=True)
        days = {(f["tag"], (f["period_end"] - f["period_start"]).days) for f in facts}
        assert days == {("Revenues", 91), ("NetIncomeLoss", 182)}

    def test_one_missing_quarter_does_not_make_half_years(self) -> None:
        ends = [dt.date(2025, 3, 31), dt.date(2025, 6, 30), dt.date(2025, 12, 31),
                dt.date(2026, 3, 31)]
        # 2025-12-31 is six months after June, but three before March.
        assert set(interim_period_days(ends).values()) == {91}

    def test_a_single_value_stays_a_quarter(self) -> None:
        assert interim_period_days([dt.date(2026, 3, 31)]) == {dt.date(2026, 3, 31): 91}

    def test_balance_sheet_instants_are_unaffected(self) -> None:
        cols = _ts("2026-06-30", "2025-12-31")
        frame = _frame({"Total Assets": [10.0, 9.0]}, cols)
        facts = facts_from_statement(frame, BALANCE_LINES, "EUR", instant=True, quarterly=True)
        assert all(f["period_start"] == f["period_end"] for f in facts)
        assert {f["fiscal_period"] for f in facts} == {"FY"}


class TestSupersededFacts:
    END = dt.date(2025, 6, 30)

    def _new(self, days: int, tag: str = "Revenues") -> dict:
        return {"tag": tag, "unit": "EUR", "period_end": self.END,
                "period_start": self.END - dt.timedelta(days=days)}

    def test_half_year_stored_as_a_quarter_is_dropped(self) -> None:
        stored = [(1, "Revenues", "EUR", self.END - dt.timedelta(days=91), self.END)]
        assert superseded_fact_ids(stored, [self._new(182)]) == [1]

    def test_same_period_is_kept_for_the_upsert(self) -> None:
        stored = [(1, "Revenues", "EUR", self.END - dt.timedelta(days=91), self.END)]
        assert superseded_fact_ids(stored, [self._new(91)]) == []

    def test_annual_facts_instants_and_other_tags_are_kept(self) -> None:
        stored = [
            (1, "Revenues", "EUR", self.END - dt.timedelta(days=364), self.END),
            (2, "Assets", "EUR", self.END, self.END),
            (3, "NetIncomeLoss", "EUR", self.END - dt.timedelta(days=91), self.END),
            (4, "Revenues", "USD", self.END - dt.timedelta(days=91), self.END),
        ]
        assert superseded_fact_ids(stored, [self._new(182)]) == []

    def test_history_yahoo_no_longer_returns_is_kept(self) -> None:
        old_end = dt.date(2020, 6, 30)
        stored = [(1, "Revenues", "EUR", old_end - dt.timedelta(days=91), old_end)]
        assert superseded_fact_ids(stored, [self._new(182)]) == []


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
