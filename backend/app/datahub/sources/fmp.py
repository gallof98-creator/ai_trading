"""Financial Modeling Prep (free plan): company reference data that EDGAR does not publish in one place
(sector, industry, market capitalisation, beta, employees).

Deliberately narrow. Reported financials come from SEC EDGAR, which is the primary source and carries the
filing time of every figure; FMP's statements do not, so they are not used. The free plan allows 250
requests a day (enforced here as a budget), covers US companies and is for personal use.

VERIFICATION STATUS: written from the vendor's documentation of its "stable" API; not yet exercised with a key.
"""
from __future__ import annotations

import pandas as pd

from ..http import ApiClient, SourceAuthError, SourceError, SourceNotFound
from ..lake import now_utc

BASE_URL = "https://financialmodelingprep.com/stable"
_FIELDS = {"companyName": "name", "exchange": "exchange", "currency": "currency", "sector": "sector", "industry": "industry", "country": "country",
           "marketCap": "market_cap", "beta": "beta", "price": "price", "fullTimeEmployees": "employees", "ipoDate": "ipo_date", "cik": "cik",
           "isin": "isin", "isEtf": "is_etf", "isActivelyTrading": "is_active"}


class FmpSource:
    name = "fmp"

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        if not api_key:
            raise SourceAuthError("No Financial Modeling Prep API key is configured")
        self._key, self.base_url = api_key, base_url

    def client(self, **kw) -> ApiClient:
        return ApiClient("Financial Modeling Prep", self.base_url, interval=1.0, params={"apikey": self._key}, **kw)

    async def profile(self, client: ApiClient, symbol: str) -> pd.DataFrame:
        """One reference-data snapshot of a company (a row for `company_profile`)."""
        body = await client.get_json("profile", {"symbol": symbol.upper()})
        if isinstance(body, dict) and body.get("Error Message"):
            raise SourceError("Financial Modeling Prep refused the request (the free plan may not include it)")
        if not isinstance(body, list):
            raise SourceError("Financial Modeling Prep returned an unexpected response")
        if not body or not isinstance(body[0], dict):
            raise SourceNotFound("Financial Modeling Prep has no profile for this symbol")
        received = now_utc()
        row = {new: body[0].get(old) for old, new in _FIELDS.items()}
        for name in ("market_cap", "beta", "price", "employees"):
            row[name] = pd.to_numeric(row[name], errors="coerce")
        for name in ("name", "exchange", "currency", "sector", "industry", "country", "ipo_date", "cik", "isin"):
            row[name] = None if row[name] in (None, "") else str(row[name])
        row.update(ts=received.floor("D"), symbol=symbol.upper(), source=self.name, received_at=received)
        return pd.DataFrame([row])
