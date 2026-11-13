"""Instrument discovery: each provider turns an exchange into a list of normalized listing records.

Providers differ in what they expose (identifiers, instrument types, which exchanges). Nothing is
assumed: a provider that cannot list an exchange raises, and the catalog records what really came back.
"""
from __future__ import annotations

import csv
import io
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ..market_data.providers import ProviderAuthError, ProviderError, ProviderRateLimited
from .exchanges import EXCHANGES, US_VENUE_MIC, Exchange

SECURITY_TYPES = ("common_stock", "preferred_stock", "etf", "reit", "adr", "depositary_receipt", "closed_end_fund", "other")
EQUITY_TYPES = frozenset({"common_stock"})  # what "equity-only" statistics may include


@dataclass
class ListingRecord:
    ticker: str
    name: str
    security_type: str
    raw_type: str
    currency: str
    provider_symbol: str
    mic: str | None = None
    isin: str | None = None
    figi: str | None = None
    share_class_figi: str | None = None


_TYPE_RULES: list[tuple[str, str]] = [
    (r"^(common stock|ordinary share|ordinary shares|stock|equity|eqs|cs)$", "common_stock"),
    (r"prefer|preference|^pfd|^ps$", "preferred_stock"),
    (r"^(etp|etf|etn|etc|exchange traded)", "etf"),
    (r"\breit\b", "reit"),
    (r"^adr$|american depositary", "adr"),
    (r"^(gdr|dr|nvdr|sdr|bdr|cdi|receipt|dutch cert|ny reg shrs)$|depositary|depository", "depositary_receipt"),
    (r"closed[- ]end", "closed_end_fund"),
]


def classify(raw_type: str, ticker: str = "", name: str = "") -> str:
    """Map a provider's instrument type to one of SECURITY_TYPES. Unknown types are 'other', never guessed to be stocks."""
    raw = (raw_type or "").strip().lower()
    for pattern, kind in _TYPE_RULES:
        if re.search(pattern, raw):
            return kind
    return "other"


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class DiscoveryProvider(ABC):
    name: str
    label: str
    verification: str = ""  # shown to the user: how far this connector has been checked against the real service

    @abstractmethod
    def list_securities(self, exchange: Exchange) -> list[ListingRecord]: ...

    def supports(self, exchange: Exchange) -> bool:
        return self.name in exchange.providers


class _Http:
    def __init__(self, http_get: Callable | None, timeout: float) -> None:
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._get, self._timeout = http_get, timeout

    def get(self, label: str, url: str, **kw):
        try:
            return self._get(url, timeout=self._timeout, **kw)
        except Exception as exc:  # exception text may contain the URL with a key
            raise ProviderError(f"{label} could not be reached ({type(exc).__name__})") from None


class FinnhubDiscovery(DiscoveryProvider):
    """Finnhub symbol lists (free key). Returns FIGI and share-class FIGI; ISIN only where the plan allows.

    VERIFICATION STATUS: written from the vendor's documentation; not yet exercised with a real key.
    """

    name, label = "finnhub", "Finnhub"
    verification = "Not yet verified against the real service: written from the vendor's documentation (free key, FIGI included, ISIN only on some plans)."
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 60.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No Finnhub API key is configured")
        self._key, self.BASE, self._http = api_key, (base_url or self.BASE).rstrip("/"), _Http(http_get, timeout)

    def list_securities(self, exchange: Exchange) -> list[ListingRecord]:
        if not self.supports(exchange):
            raise ProviderError(f"Finnhub has no code configured for {exchange.name}")
        r = self._http.get("Finnhub", f"{self.BASE}/stock/symbol", params={"exchange": exchange.providers["finnhub"]},
                           headers={"X-Finnhub-Token": self._key})
        if r.status_code in (401, 403):
            raise ProviderAuthError("Finnhub rejected the API key, or the plan does not include this exchange")
        if r.status_code == 429:
            raise ProviderRateLimited("Finnhub: too many requests")
        if r.status_code != 200:
            raise ProviderError(f"Finnhub answered with HTTP {r.status_code}")
        try:
            rows = r.json()
        except Exception:
            raise ProviderError("Finnhub returned a response that is not JSON") from None
        if not isinstance(rows, list):
            raise ProviderError("Finnhub returned an unexpected response")
        out = []
        for row in rows:
            symbol = _clean(row.get("symbol"))
            ticker = _clean(row.get("displaySymbol")) or symbol
            if not symbol or not ticker:
                continue
            suffix = "." + exchange.providers["finnhub"]
            if exchange.code != "US" and ticker.upper().endswith(suffix.upper()):
                ticker = ticker[: -len(suffix)]
            raw = _clean(row.get("type")) or ""
            out.append(ListingRecord(ticker=ticker.upper(), name=_clean(row.get("description")) or "", security_type=classify(raw), raw_type=raw,
                                     currency=(_clean(row.get("currency")) or exchange.currency).upper(), provider_symbol=symbol,
                                     mic=_clean(row.get("mic")) or exchange.mic, isin=_clean(row.get("isin")), figi=_clean(row.get("figi")),
                                     share_class_figi=_clean(row.get("shareClassFIGI"))))
        return out


class AlphaVantageDiscovery(DiscoveryProvider):
    """Alpha Vantage's file of all active US listings (free key, one request). No ISIN or FIGI:
    those are added afterwards through OpenFIGI. Verified live with the vendor's demo key."""

    name, label = "alphavantage", "Alpha Vantage"
    verification = "Verified against the real service. Lists every active US listing with one request; identifiers are added afterwards through OpenFIGI."
    BASE = "https://www.alphavantage.co"

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 90.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No Alpha Vantage API key is configured")
        self._key, self.BASE, self._http = api_key, (base_url or self.BASE).rstrip("/"), _Http(http_get, timeout)

    @staticmethod
    def _kind(asset_type: str, symbol: str) -> tuple[str, str]:
        """The file only says Stock or ETF; the ticker pattern separates share classes from preferreds, warrants, units and rights."""
        if asset_type == "ETF":
            return "etf", "ETF"
        if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", symbol):
            return "common_stock", "Stock"
        if "-P-" in symbol or re.search(r"-P[A-Z]?$", symbol):
            return "preferred_stock", "Stock (preferred series)"
        return "other", "Stock (warrant, unit, right or other series)"

    def list_securities(self, exchange: Exchange) -> list[ListingRecord]:
        if exchange.code != "US":
            raise ProviderError("Alpha Vantage lists the US market only")
        r = self._http.get("Alpha Vantage", f"{self.BASE}/query", params={"function": "LISTING_STATUS", "apikey": self._key})
        if r.status_code != 200:
            raise ProviderError(f"Alpha Vantage answered with HTTP {r.status_code}")
        text = r.text
        if text.lstrip().startswith("{"):
            message = " ".join(str(v) for v in json.loads(text).values()).lower()
            if "rate limit" in message or "requests per day" in message:
                raise ProviderRateLimited("Alpha Vantage: the free plan's daily request limit is reached")
            raise ProviderAuthError("Alpha Vantage did not accept the API key")
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol or row.get("status") != "Active":
                continue
            kind, raw = self._kind(row.get("assetType", ""), symbol)
            # Share classes are written with a dot on this platform (BRK-B -> BRK.B); other series keep the provider's symbol.
            ticker = symbol.replace("-", ".") if re.fullmatch(r"[A-Z]{1,5}-[A-Z]", symbol) else symbol
            out.append(ListingRecord(ticker=ticker, name=(row.get("name") or "").strip(), security_type=kind, raw_type=raw,
                                     currency="USD", provider_symbol=symbol, mic=US_VENUE_MIC.get((row.get("exchange") or "").upper())))
        return out


class EodhdDiscovery(DiscoveryProvider):
    """EODHD exchange symbol lists (paid plan), with ISIN. Not verified live: the demo key cannot call this endpoint."""

    name, label = "eodhd", "EODHD"
    verification = "Not yet verified against the real service: symbol lists need a paid key. Provides ISIN."
    BASE = "https://eodhd.com/api"

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 90.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No EODHD API key is configured")
        self._key, self.BASE, self._http = api_key, (base_url or self.BASE).rstrip("/"), _Http(http_get, timeout)

    def list_securities(self, exchange: Exchange) -> list[ListingRecord]:
        if not self.supports(exchange):
            raise ProviderError(f"EODHD has no code configured for {exchange.name}")
        code = exchange.providers["eodhd"]
        r = self._http.get("EODHD", f"{self.BASE}/exchange-symbol-list/{code}", params={"api_token": self._key, "fmt": "json"})
        if r.status_code in (401, 403):
            raise ProviderAuthError("EODHD rejected the API key, or the plan does not include symbol lists")
        if r.status_code in (402, 429):
            raise ProviderRateLimited("EODHD: the plan's request limit is used up")
        if r.status_code != 200:
            raise ProviderError(f"EODHD answered with HTTP {r.status_code}")
        try:
            rows = r.json()
        except Exception:
            raise ProviderError("EODHD returned a response that is not JSON") from None
        if not isinstance(rows, list):
            raise ProviderError("EODHD returned an unexpected response")
        out = []
        for row in rows:
            symbol = _clean(row.get("Code"))
            if not symbol:
                continue
            raw = _clean(row.get("Type")) or ""
            venue = (_clean(row.get("Exchange")) or "").upper()
            out.append(ListingRecord(ticker=symbol.upper().replace("-", ".") if exchange.code == "US" else symbol.upper(), name=_clean(row.get("Name")) or "",
                                     security_type=classify(raw), raw_type=raw, currency=(_clean(row.get("Currency")) or exchange.currency).upper(),
                                     provider_symbol=f"{symbol}.{code}", mic=US_VENUE_MIC.get(venue, exchange.mic) if exchange.code == "US" else exchange.mic,
                                     isin=_clean(row.get("Isin"))))
        return out


DISCOVERY_CLASSES: dict[str, type[DiscoveryProvider]] = {"finnhub": FinnhubDiscovery, "alphavantage": AlphaVantageDiscovery, "eodhd": EodhdDiscovery}


class OpenFigi:
    """OpenFIGI mapping API (free, no key needed; a free key raises the limits).
    Verified live: ISIN and ticker lookups return FIGI, composite FIGI, share-class FIGI and security type."""

    URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self, api_key: str = "", http_post: Callable | None = None, timeout: float = 60.0, url: str | None = None) -> None:
        if http_post is None:
            import httpx

            http_post = httpx.post
        self._post, self._timeout, self._key, self.URL = http_post, timeout, api_key, url or self.URL
        self.batch_size = 100 if api_key else 10
        self.min_interval = 0.3 if api_key else 2.6  # 25 requests per 6 s with a key, 25 per minute without

    def lookup(self, jobs: list[dict]) -> list[dict | None]:
        """jobs: [{"isin": ..} or {"ticker": .., } plus "exch": OpenFIGI exchange code]. Returns one match or None per job, in order."""
        if len(jobs) > self.batch_size:
            raise ValueError(f"At most {self.batch_size} jobs per request")
        body = []
        for j in jobs:
            item = {"idType": "ID_ISIN", "idValue": j["isin"]} if j.get("isin") else {"idType": "TICKER", "idValue": j["ticker"]}
            if j.get("exch"):
                item["exchCode"] = j["exch"]
            body.append(item)
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["X-OPENFIGI-APIKEY"] = self._key
        try:
            r = self._post(self.URL, json=body, headers=headers, timeout=self._timeout)
        except Exception as exc:
            raise ProviderError(f"OpenFIGI could not be reached ({type(exc).__name__})") from None
        if r.status_code == 429:
            raise ProviderRateLimited("OpenFIGI: too many requests")
        if r.status_code in (401, 403):
            raise ProviderAuthError("OpenFIGI rejected the API key")
        if r.status_code != 200:
            raise ProviderError(f"OpenFIGI answered with HTTP {r.status_code}")
        results = r.json()
        if not isinstance(results, list) or len(results) != len(jobs):
            raise ProviderError("OpenFIGI returned an unexpected response")
        out: list[dict | None] = []
        for res in results:
            data = res.get("data") if isinstance(res, dict) else None
            # Several matches mean the lookup was ambiguous: recording one of them would be a guess.
            out.append(data[0] if data and len(data) == 1 else None)
        return out
