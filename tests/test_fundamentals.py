"""Unit tests for the pure SEC parsing helpers — no network, no database."""

from __future__ import annotations

import datetime as dt

from fintracker.ingest.fundamentals import (
    extract_facts,
    iter_recent_filings,
    resolve_cik,
    select_new_filings,
)

COMPANY_TICKERS = {
    "0": {"cik_str": 731766, "ticker": "UNH", "title": "UNITEDHEALTH GROUP INC"},
    "1": {"cik_str": 1880319, "ticker": "PRM", "title": "Perimeter Solutions"},
    "2": {"cik_str": 1001085, "ticker": "BN", "title": "BROOKFIELD CORP"},
}

SUBMISSIONS = {
    "cik": "731766",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000731766-26-000042",
                "0000731766-26-000039",
                "0000731766-26-000031",
            ],
            "form": ["10-Q", "8-K", "10-K"],
            "filingDate": ["2026-07-12", "2026-06-30", "2026-02-20"],
        }
    },
}

COMPANY_FACTS = {
    "cik": 731766,
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2026-04-01",
                            "end": "2026-06-30",
                            "val": 99_800_000_000,
                            "fy": 2026,
                            "fp": "Q2",
                            "form": "10-Q",
                            "accn": "0000731766-26-000042",
                            "filed": "2026-07-12",
                        },
                        # Same period restated in a later filing — must win.
                        {
                            "start": "2026-04-01",
                            "end": "2026-06-30",
                            "val": 99_900_000_000,
                            "fy": 2026,
                            "fp": "Q2",
                            "form": "10-Q/A",
                            "accn": "0000731766-26-000050",
                            "filed": "2026-08-01",
                        },
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        # Instant fact: no "start".
                        {
                            "end": "2026-06-30",
                            "val": 302_000_000_000,
                            "fy": 2026,
                            "fp": "Q2",
                            "form": "10-Q",
                            "accn": "0000731766-26-000042",
                            "filed": "2026-07-12",
                        },
                        # Malformed item: no value — must be skipped.
                        {"end": "2026-03-31", "fy": 2026, "fp": "Q1", "form": "10-Q"},
                    ]
                }
            },
        }
    },
}


class TestResolveCik:
    def test_resolves_and_zero_pads(self) -> None:
        assert resolve_cik("UNH", COMPANY_TICKERS) == "0000731766"

    def test_is_case_insensitive(self) -> None:
        assert resolve_cik("prm", COMPANY_TICKERS) == "0001880319"

    def test_unknown_ticker_returns_none(self) -> None:
        assert resolve_cik("NOPE", COMPANY_TICKERS) is None


class TestSubmissionsFiltering:
    def test_iter_recent_filings_flattens_parallel_arrays(self) -> None:
        filings = iter_recent_filings(SUBMISSIONS)
        assert len(filings) == 3
        assert filings[0] == {
            "accession_no": "0000731766-26-000042",
            "form": "10-Q",
            "filed_at": dt.date(2026, 7, 12),
        }

    def test_select_new_filings_keeps_only_unseen_financial_forms(self) -> None:
        known = {"0000731766-26-000031"}  # the 10-K was already processed
        new = select_new_filings(SUBMISSIONS, known)
        # The 8-K is not a financial form; the known 10-K is filtered out.
        assert [f["accession_no"] for f in new] == ["0000731766-26-000042"]
        assert new[0]["form"] == "10-Q"

    def test_select_new_filings_empty_feed(self) -> None:
        assert select_new_filings({}, set()) == []


class TestExtractFacts:
    def test_extracts_curated_tags_only(self) -> None:
        facts = extract_facts(COMPANY_FACTS, "us-gaap", ("Revenues",))
        assert {f["tag"] for f in facts} == {"Revenues"}

    def test_duration_fact_keeps_start_and_end(self) -> None:
        (fact,) = extract_facts(COMPANY_FACTS, "us-gaap", ("Revenues",))
        assert fact["period_start"] == dt.date(2026, 4, 1)
        assert fact["period_end"] == dt.date(2026, 6, 30)

    def test_same_period_collapses_to_latest_filed(self) -> None:
        (fact,) = extract_facts(COMPANY_FACTS, "us-gaap", ("Revenues",))
        assert fact["value"] == 99_900_000_000
        assert fact["form"] == "10-Q/A"

    def test_instant_fact_uses_end_as_start(self) -> None:
        (fact,) = extract_facts(COMPANY_FACTS, "us-gaap", ("Assets",))
        assert fact["period_start"] == fact["period_end"] == dt.date(2026, 6, 30)

    def test_items_without_value_are_skipped(self) -> None:
        facts = extract_facts(COMPANY_FACTS, "us-gaap", ("Assets",))
        assert len(facts) == 1

    def test_missing_taxonomy_returns_empty(self) -> None:
        assert extract_facts(COMPANY_FACTS, "ifrs-full", ("Revenue",)) == []
