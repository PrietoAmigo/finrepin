"""Thin SEC EDGAR HTTP client (JSON APIs only — no documents are downloaded).

The SEC requires a descriptive User-Agent containing a contact email and asks
clients to stay well under 10 requests/second; we send the configured
`SEC_USER_AGENT` and pause briefly between requests.
"""

from __future__ import annotations

import time
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from fintracker.config import get_settings

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_REQUEST_PAUSE_SECONDS = 0.5


class SecClient:
    def __init__(self, user_agent: str | None = None) -> None:
        ua = user_agent or get_settings().sec_user_agent
        if not ua:
            raise RuntimeError(
                "SEC_USER_AGENT is not set; the SEC returns 403 without a "
                "descriptive User-Agent that includes a contact email."
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def _get_json(self, url: str) -> dict[str, Any]:
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        time.sleep(_REQUEST_PAUSE_SECONDS)
        data: dict[str, Any] = resp.json()
        return data

    def company_tickers(self) -> dict[str, Any]:
        """Ticker -> CIK mapping file for the whole EDGAR universe."""
        return self._get_json(COMPANY_TICKERS_URL)

    def submissions(self, cik: str) -> dict[str, Any]:
        """Recent filings feed for one company (10-digit zero-padded CIK)."""
        return self._get_json(SUBMISSIONS_URL.format(cik=cik))

    def company_facts(self, cik: str) -> dict[str, Any]:
        """All XBRL facts for one company (10-digit zero-padded CIK)."""
        return self._get_json(COMPANY_FACTS_URL.format(cik=cik))
