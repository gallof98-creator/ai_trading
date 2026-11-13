"""Market-data vendors with an official API, used with the user's own key.

Only vendors that offer a documented, licensed API belong here. Websites without one (which
would have to be scraped or accessed while posing as a browser) are deliberately not supported.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, datetime, timezone

import pandas as pd

from ..domain.models import Instrument
from ..instruments.registry import MARKETS, market_of
from .quality import DataKind
from .sources import Dataset
from .timeframes import OHLCV


class ProviderError(Exception):
    """A message that is safe to show to the user (never contains the API key)."""


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class SymbolNotFound(ProviderError):
    pass


class PriceProvider(ABC):
    name: str
    label: str
    markets: frozenset[str] = frozenset()  # markets it can download prices for
    listing_markets: frozenset[str] = frozenset()  # markets whose symbol list it can provide
    pricing: str = ""  # shown to the user: what it costs and its limits
    signup_url: str = ""
    min_interval: float = 0.1  # seconds between requests that keeps within the plan's rate limit

    @abstractmethod
    def daily_bars(self, instrument: Instrument, start: date | None = None, end: date | None = None) -> Dataset: ...

    @abstractmethod
    def list_symbols(self, market: str) -> list[dict]:
        """[{ticker, name, type ('stock'|'etf'), currency, exchange}] for one market."""


class EodhdProvider(PriceProvider):
    """EOD Historical Data (eodhd.com): end-of-day prices for US and European exchanges.

    Prices are adjusted for splits and dividends by scaling each bar's open/high/low/close with
    adjusted_close / close, so a series has no artificial jumps on corporate-action dates.
    Volume is left as reported.

    VERIFICATION STATUS: the price endpoint was checked live with the vendor's public demo key
    (including continuity across a stock split). The symbol-list endpoint needs a real key and
    has only been tested against responses of the documented shape.
    """

    name = "eodhd"
    label = "EODHD"
    markets = frozenset(MARKETS)
    listing_markets = frozenset(MARKETS)
    pricing = "Paid plan needed for full coverage (the free plan allows about 20 requests a day and one year of history)."
    signup_url = "https://eodhd.com"
    BASE = "https://eodhd.com/api"
    # US venues worth importing by default; OTC / pink-sheet listings are excluded.
    US_VENUES = {"NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS"}

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 30.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No EODHD API key is configured")
        self._key = api_key
        self.BASE = (base_url or self.BASE).rstrip("/")
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._get, self._timeout = http_get, timeout

    def symbol(self, instrument: Instrument) -> str:
        market = market_of(instrument)
        if market is None:
            raise SymbolNotFound(f"{instrument.id} is not a stock or ETF on a supported market")
        ticker = instrument.symbol.replace(".", "-") if market == "US" else instrument.symbol  # BRK.B -> BRK-B
        return f"{ticker}.{MARKETS[market]['eodhd']}"

    def _request(self, path: str, params: dict) -> list:
        try:
            response = self._get(f"{self.BASE}/{path}", params={**params, "api_token": self._key, "fmt": "json"}, timeout=self._timeout)
        except Exception as exc:  # the exception text can contain the full URL including the key
            raise ProviderError(f"EODHD could not be reached ({type(exc).__name__})") from None
        code = response.status_code
        if code in (401, 403):
            raise ProviderAuthError("EODHD rejected the API key, or the plan does not include this data")
        if code == 402:
            raise ProviderRateLimited("EODHD: the plan's request limit is used up")
        if code == 429:
            raise ProviderRateLimited("EODHD: too many requests")
        if code == 404:
            raise SymbolNotFound("EODHD has no data for this symbol")
        if code != 200:
            raise ProviderError(f"EODHD answered with HTTP {code}")
        try:
            data = response.json()
        except Exception:
            raise ProviderError("EODHD returned a response that is not JSON") from None
        if not isinstance(data, list):
            raise ProviderError("EODHD returned an unexpected response")
        return data

    def daily_bars(self, instrument: Instrument, start: date | None = None, end: date | None = None) -> Dataset:
        params: dict = {"period": "d", "order": "a"}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        rows = self._request(f"eod/{self.symbol(instrument)}", params)
        records = []
        for r in rows:
            try:
                o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
                adj = float(r.get("adjusted_close") or c)
                if min(o, h, l, c, adj) <= 0:
                    continue
                factor = adj / c
                records.append((pd.Timestamp(r["date"], tz="UTC"), o * factor, h * factor, l * factor, adj, float(r.get("volume") or 0)))
            except (KeyError, TypeError, ValueError):
                continue  # malformed row: dropped here, counted later by validation as a missing bar
        if not records:
            raise SymbolNotFound("EODHD returned no usable bars for this symbol")
        frame = pd.DataFrame(records, columns=["ts", *OHLCV]).set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return Dataset(frame, DataKind.HISTORICAL, "eodhd")

    def list_symbols(self, market: str) -> list[dict]:
        if market not in MARKETS:
            raise ProviderError(f"Unknown market {market}")
        out = []
        for r in self._request(f"exchange-symbol-list/{MARKETS[market]['eodhd']}", {}):
            kind = {"Common Stock": "stock", "ETF": "etf"}.get(str(r.get("Type", "")))
            code = str(r.get("Code", "")).strip().upper()
            if kind is None or not code:
                continue
            if market == "US" and str(r.get("Exchange", "")).upper() not in self.US_VENUES:
                continue
            if r.get("Currency") and str(r["Currency"]).upper() != MARKETS[market]["currency"]:
                continue  # e.g. a USD line on a European venue: skip rather than value it in the wrong currency
            out.append({"ticker": code.replace("-", ".") if market == "US" else code, "name": str(r.get("Name") or ""), "type": kind,
                        "currency": MARKETS[market]["currency"], "exchange": str(r.get("Exchange") or "")})
        return out


class TiingoProvider(PriceProvider):
    """Tiingo end-of-day prices for US stocks and ETFs (free API key). Uses the vendor's adjusted
    open/high/low/close, which account for splits and dividends.

    VERIFICATION STATUS: written from the vendor's documented endpoint; not yet exercised with a real
    key. Use "Test connection" first.
    """

    name = "tiingo"
    label = "Tiingo"
    markets = frozenset({"US"})
    pricing = "Free key: about 50 requests an hour, 1,000 a day and 500 different symbols a month. US stocks and ETFs only."
    signup_url = "https://www.tiingo.com"
    min_interval = 75.0  # stays under the free plan's hourly allowance
    BASE = "https://api.tiingo.com"

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 30.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No Tiingo API key is configured")
        self._key = api_key
        self.BASE = (base_url or self.BASE).rstrip("/")
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._get, self._timeout = http_get, timeout

    def list_symbols(self, market: str) -> list[dict]:
        raise ProviderError("Tiingo does not provide a symbol list here; import the US list with Alpha Vantage or add tickers by hand")

    def daily_bars(self, instrument: Instrument, start: date | None = None, end: date | None = None) -> Dataset:
        if market_of(instrument) != "US":
            raise SymbolNotFound(f"Tiingo covers US stocks only; {instrument.id} is not one")
        params = {"format": "json"}
        if start:
            params["startDate"] = start.isoformat()
        if end:
            params["endDate"] = end.isoformat()
        ticker = instrument.symbol.replace(".", "-").lower()
        try:  # the key travels in a header, never in the URL
            response = self._get(f"{self.BASE}/tiingo/daily/{ticker}/prices", params=params, timeout=self._timeout,
                                 headers={"Authorization": f"Token {self._key}", "Content-Type": "application/json"})
        except Exception as exc:
            raise ProviderError(f"Tiingo could not be reached ({type(exc).__name__})") from None
        code = response.status_code
        if code in (401, 403):
            raise ProviderAuthError("Tiingo rejected the API key")
        if code == 429:
            raise ProviderRateLimited("Tiingo: the plan's request allowance is used up for now")
        if code == 404:
            raise SymbolNotFound("Tiingo has no data for this symbol")
        if code != 200:
            raise ProviderError(f"Tiingo answered with HTTP {code}")
        try:
            rows = response.json()
        except Exception:
            raise ProviderError("Tiingo returned a response that is not JSON") from None
        if isinstance(rows, dict) and "allocation" in str(rows.get("detail", "")).lower():
            raise ProviderRateLimited("Tiingo: the plan's request allowance is used up for now")
        if not isinstance(rows, list):
            raise ProviderError("Tiingo returned an unexpected response")
        records = []
        for r in rows:
            try:
                o, h, l, c = (float(r.get(f"adj{k}") if r.get(f"adj{k}") is not None else r[k.lower()]) for k in ("Open", "High", "Low", "Close"))
                if min(o, h, l, c) <= 0:
                    continue
                volume = r.get("adjVolume") if r.get("adjVolume") is not None else r.get("volume")
                records.append((pd.Timestamp(str(r["date"])[:10], tz="UTC"), o, h, l, c, float(volume or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        if not records:
            raise SymbolNotFound("Tiingo returned no usable bars for this symbol")
        frame = pd.DataFrame(records, columns=["ts", *OHLCV]).set_index("ts").sort_index()
        return Dataset(frame[~frame.index.duplicated(keep="last")], DataKind.HISTORICAL, "tiingo")


class AlphaVantageProvider(PriceProvider):
    """Alpha Vantage (free API key): the list of all active US listings, and US daily prices.

    Limits of the free plan matter here: about 25 requests a day, so it is practical for the symbol
    list (one request) and a handful of price series, not for a whole market. Prices from the free
    endpoint are NOT adjusted for splits or dividends, and the free plan may return only the most
    recent 100 days; both facts are recorded in the stored series' source label.

    VERIFICATION STATUS: response formats checked live with the vendor's public demo key (daily
    series and the listing file). Behaviour of a personal free key (history length) is not verified.
    """

    name = "alphavantage"
    label = "Alpha Vantage"
    markets = frozenset({"US"})
    listing_markets = frozenset({"US"})
    pricing = "Free key: about 25 requests a day. Good for importing the US symbol list; too limited for downloading many price series. Prices are unadjusted."
    signup_url = "https://www.alphavantage.co/support/#api-key"
    min_interval = 13.0
    BASE = "https://www.alphavantage.co"

    def __init__(self, api_key: str, http_get: Callable | None = None, timeout: float = 60.0, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderAuthError("No Alpha Vantage API key is configured")
        self._key = api_key
        self.BASE = (base_url or self.BASE).rstrip("/")
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._get, self._timeout = http_get, timeout

    def _request(self, params: dict):
        try:
            response = self._get(f"{self.BASE}/query", params={**params, "apikey": self._key}, timeout=self._timeout)
        except Exception as exc:  # exception text can include the URL with the key
            raise ProviderError(f"Alpha Vantage could not be reached ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ProviderError(f"Alpha Vantage answered with HTTP {response.status_code}")
        return response

    @staticmethod
    def _raise_for_message(data: dict) -> None:
        """Alpha Vantage reports problems inside a 200 response."""
        text = " ".join(str(data.get(k, "")) for k in ("Information", "Note", "Error Message")).strip()
        if not text:
            return
        low = text.lower()
        if "premium" in low and "rate limit" not in low and "requests per day" not in low:
            raise _PremiumOnly(text[:200])
        if "rate limit" in low or "requests per day" in low or "call frequency" in low:
            raise ProviderRateLimited("Alpha Vantage: the free plan's daily request limit is reached")
        if "demo" in low or "api key" in low or "apikey" in low:
            raise ProviderAuthError("Alpha Vantage did not accept the API key")
        if "Error Message" in data:
            raise SymbolNotFound("Alpha Vantage has no data for this symbol")
        raise ProviderError("Alpha Vantage refused the request")

    def _series(self, symbol: str, size: str) -> dict:
        try:
            data = self._request({"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": size}).json()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Alpha Vantage returned a response that is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("Alpha Vantage returned an unexpected response")
        if "Time Series (Daily)" not in data:
            self._raise_for_message(data)
            raise ProviderError("Alpha Vantage returned an unexpected response")
        return data["Time Series (Daily)"]

    def daily_bars(self, instrument: Instrument, start: date | None = None, end: date | None = None) -> Dataset:
        if market_of(instrument) != "US":
            raise SymbolNotFound(f"Alpha Vantage is used here for US stocks only; {instrument.id} is not one")
        symbol = instrument.symbol.replace(".", "-")
        source = "alphavantage:unadjusted"
        try:
            series = self._series(symbol, "full")
        except _PremiumOnly:
            series = self._series(symbol, "compact")  # the free plan's most recent 100 days
            source = "alphavantage:unadjusted:last100"
        records = []
        for day, r in series.items():
            try:
                o, h, l, c = (float(r[k]) for k in ("1. open", "2. high", "3. low", "4. close"))
                ts = pd.Timestamp(day, tz="UTC")
                if min(o, h, l, c) <= 0 or (start and ts.date() < start) or (end and ts.date() > end):
                    continue
                records.append((ts, o, h, l, c, float(r.get("5. volume") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        if not records:
            raise SymbolNotFound("Alpha Vantage returned no usable bars for this symbol")
        frame = pd.DataFrame(records, columns=["ts", *OHLCV]).set_index("ts").sort_index()
        return Dataset(frame[~frame.index.duplicated(keep="last")], DataKind.HISTORICAL, source)

    def list_symbols(self, market: str) -> list[dict]:
        import csv
        import io
        import re

        if market != "US":
            raise ProviderError("Alpha Vantage provides the symbol list for the US market only")
        text = self._request({"function": "LISTING_STATUS"}).text  # active listings are the default
        if text.lstrip().startswith("{"):
            import json

            self._raise_for_message(json.loads(text))
            raise ProviderError("Alpha Vantage returned an unexpected response")
        out = []
        for r in csv.DictReader(io.StringIO(text)):
            kind = {"Stock": "stock", "ETF": "etf"}.get(r.get("assetType", ""))
            code = (r.get("symbol") or "").strip().upper()
            # Ordinary shares and share classes only (BRK-B); warrants, units and preferred series are skipped.
            if kind is None or r.get("status") != "Active" or not re.fullmatch(r"[A-Z]{1,5}(-[A-Z]{1,2})?", code):
                continue
            out.append({"ticker": code.replace("-", "."), "name": r.get("name") or "", "type": kind, "currency": "USD", "exchange": r.get("exchange") or ""})
        return out


class _PremiumOnly(ProviderError):
    pass


PROVIDER_CLASSES: dict[str, type[PriceProvider]] = {"tiingo": TiingoProvider, "alphavantage": AlphaVantageProvider, "eodhd": EodhdProvider}


class DownloadJob:
    """Background download of daily history for many instruments, one request at a time.

    Each instrument is stored in its own transaction; a failure is recorded and the job moves on.
    An authentication failure stops the job (every further request would fail the same way).
    """

    def __init__(self, provider: PriceProvider, instruments: list[Instrument], store_one: Callable[[Instrument, Dataset], int],
                 years: int = 10, min_interval: float | None = None, sleep: Callable[[float], None] = time.sleep,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.provider, self.instruments, self._store_one = provider, instruments, store_one
        self.years, self._sleep, self._today = years, sleep, today
        self.min_interval = provider.min_interval if min_interval is None else min_interval
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.state = {"status": "pending", "provider": provider.name, "total": len(instruments), "done": 0, "stored": 0, "bars": 0,
                      "failed": [], "failed_count": 0, "current": None, "started_at": None, "finished_at": None, "message": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "failed": list(self.state["failed"])}

    def cancel(self) -> None:
        self._cancel.set()

    def _update(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def run(self) -> None:
        self._update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        end = self._today()
        start = end.replace(year=end.year - self.years) if self.years else None
        outcome, message = "finished", ""
        for index, inst in enumerate(self.instruments):
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelled by the user"
                break
            self._update(current=inst.id)
            error = None
            limited = False
            for attempt in range(3):
                try:
                    bars = self._store_one(inst, self.provider.daily_bars(inst, start, end))
                    with self._lock:
                        self.state["stored"] += 1
                        self.state["bars"] += bars
                    error, limited = None, False
                    break
                except ProviderRateLimited as exc:
                    error, limited = str(exc), True
                    if attempt < 2:
                        self._sleep(30.0 * (attempt + 1))
                except ProviderAuthError as exc:
                    outcome, message, error = "failed", str(exc), str(exc)
                    break
                except (ProviderError, ValueError) as exc:
                    error = str(exc)
                    break
                except Exception as exc:  # database or unexpected error: record it, keep the job alive
                    error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    break
            with self._lock:
                self.state["done"] += 0 if limited else 1
                if error and not limited:
                    self.state["failed_count"] += 1
                    if len(self.state["failed"]) < 300:
                        self.state["failed"].append({"instrument_id": inst.id, "reason": error})
            if outcome == "failed":
                break
            if limited:  # the allowance is exhausted: stop here; "download missing" resumes from this point later
                outcome, message = "stopped", f"{error}. Start 'Download missing' again later: it resumes where this stopped."
                break
            if index < len(self.instruments) - 1:
                self._sleep(self.min_interval)
        self._update(status=outcome, message=message, current=None, finished_at=datetime.now(timezone.utc).isoformat())
