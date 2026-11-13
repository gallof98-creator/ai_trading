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
    needs_secret: bool = False  # the vendor issues a key id plus a secret
    bulk_size: int = 1  # instruments that one request can cover

    def daily_bars_bulk(self, instruments: list[Instrument], start: date | None = None, end: date | None = None) -> dict[str, Dataset]:
        """History for several instruments at once, keyed by instrument id. Instruments the vendor has
        no data for are simply absent from the result. Default: one request per instrument."""
        out = {}
        for inst in instruments:
            try:
                out[inst.id] = self.daily_bars(inst, start, end)
            except SymbolNotFound:
                continue
        return out

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


class AlpacaProvider(PriceProvider):
    """Alpaca market data (free account): daily bars for US stocks and ETFs, many symbols per request,
    adjusted for splits and dividends. The free plan serves full-market data older than 15 minutes,
    so requests stop at yesterday.

    VERIFICATION STATUS: written from the vendor's documentation; not yet exercised with real keys.
    """

    name = "alpaca"
    label = "Alpaca"
    markets = frozenset({"US"})
    pricing = "Free account (paper trading is enough): about 200 requests a minute, history from 2016. US stocks and ETFs only."
    signup_url = "https://alpaca.markets"
    min_interval = 0.35
    needs_secret = True
    bulk_size = 50
    BASE = "https://data.alpaca.markets"

    def __init__(self, api_key: str, api_secret: str = "", http_get: Callable | None = None, timeout: float = 60.0, base_url: str | None = None) -> None:
        if not api_key or not api_secret:
            raise ProviderAuthError("Alpaca needs both the API key id and the secret key")
        self._key, self._secret = api_key, api_secret
        self.BASE = (base_url or self.BASE).rstrip("/")
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._get, self._timeout = http_get, timeout

    def list_symbols(self, market: str) -> list[dict]:
        raise ProviderError("Alpaca is used here for prices only; build the US list from the Global Markets catalog")

    def daily_bars(self, instrument: Instrument, start: date | None = None, end: date | None = None) -> Dataset:
        out = self.daily_bars_bulk([instrument], start, end)
        if instrument.id not in out:
            raise SymbolNotFound("Alpaca has no data for this symbol")
        return out[instrument.id]

    def daily_bars_bulk(self, instruments: list[Instrument], start: date | None = None, end: date | None = None) -> dict[str, Dataset]:
        if len(instruments) > self.bulk_size:
            raise ValueError(f"At most {self.bulk_size} instruments per request")
        by_symbol = {i.symbol: i for i in instruments if market_of(i) == "US"}
        if not by_symbol:
            return {}
        yesterday = datetime.now(timezone.utc).date() - pd.Timedelta(days=1).to_pytimedelta()
        params = {"symbols": ",".join(by_symbol), "timeframe": "1Day", "adjustment": "all", "feed": "sip", "sort": "asc", "limit": 10000,
                  "start": (start or date(2016, 1, 1)).isoformat(), "end": min(end or yesterday, yesterday).isoformat()}
        collected: dict[str, list] = {}
        for _ in range(500):  # pages; bounded so a faulty token cannot loop forever
            try:
                response = self._get(f"{self.BASE}/v2/stocks/bars", params=params, timeout=self._timeout,
                                     headers={"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret})
            except Exception as exc:
                raise ProviderError(f"Alpaca could not be reached ({type(exc).__name__})") from None
            code = response.status_code
            if code in (401, 403):
                raise ProviderAuthError("Alpaca rejected the keys, or the plan does not include this data")
            if code == 429:
                raise ProviderRateLimited("Alpaca: too many requests")
            if code != 200:
                raise ProviderError(f"Alpaca answered with HTTP {code}")
            try:
                body = response.json()
            except Exception:
                raise ProviderError("Alpaca returned a response that is not JSON") from None
            if not isinstance(body, dict) or not isinstance(body.get("bars"), dict):
                raise ProviderError("Alpaca returned an unexpected response")
            for symbol, bars in body["bars"].items():
                collected.setdefault(symbol, []).extend(bars or [])
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
            time.sleep(0)  # pacing between pages is left to the caller's rate budget
        out: dict[str, Dataset] = {}
        for symbol, bars in collected.items():
            inst = by_symbol.get(symbol)
            if inst is None:
                continue
            records = []
            for b in bars:
                try:
                    o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
                    if min(o, h, l, c) <= 0:
                        continue
                    # Daily bars are stamped at midnight New York time; the trading date is the New York date.
                    day = pd.Timestamp(b["t"]).tz_convert("America/New_York").date()
                    records.append((pd.Timestamp(day, tz="UTC"), o, h, l, c, float(b.get("v") or 0)))
                except (KeyError, TypeError, ValueError):
                    continue
            if records:
                frame = pd.DataFrame(records, columns=["ts", *OHLCV]).set_index("ts").sort_index()
                out[inst.id] = Dataset(frame[~frame.index.duplicated(keep="last")], DataKind.HISTORICAL, "alpaca")
        return out


class _PremiumOnly(ProviderError):
    pass


PROVIDER_CLASSES: dict[str, type[PriceProvider]] = {"alpaca": AlpacaProvider, "tiingo": TiingoProvider, "alphavantage": AlphaVantageProvider,
                                                    "eodhd": EodhdProvider}


class NeedsFullHistory(Exception):
    """Raised by the storage step when an incremental update cannot be appended safely."""


class DownloadJob:
    """Background download of daily history for many instruments.

    Instruments are requested in batches when the provider supports it, otherwise one at a time.
    Each instrument is stored in its own transaction; a failure is recorded and the job moves on.
    An authentication failure stops the job, and so does an exhausted allowance (resumable later).

    mode "full" replaces each history. mode "update" fetches only recent bars and appends them; when
    the overlap with the stored history shows that the vendor re-adjusted past prices (after a split
    or dividend), that instrument's whole history is downloaded again instead.
    """

    def __init__(self, provider: PriceProvider, instruments: list[Instrument], store_one: Callable[..., int],
                 years: int = 10, min_interval: float | None = None, sleep: Callable[[float], None] = time.sleep,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date(), mode: str = "full",
                 last_dates: dict[str, date] | None = None) -> None:
        self.provider, self.instruments, self._store_one = provider, instruments, store_one
        self.years, self._sleep, self._today, self.mode, self.last_dates = years, sleep, today, mode, last_dates or {}
        self.min_interval = provider.min_interval if min_interval is None else min_interval
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.requests = 0
        self.state = {"status": "pending", "provider": provider.name, "mode": mode, "total": len(instruments), "done": 0, "stored": 0, "bars": 0,
                      "readjusted": 0, "failed": [], "failed_count": 0, "current": None, "started_at": None, "finished_at": None, "message": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "failed": list(self.state["failed"])}

    def cancel(self) -> None:
        self._cancel.set()

    def _update(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def _fail(self, inst: Instrument, reason: str) -> None:
        with self._lock:
            self.state["failed_count"] += 1
            if len(self.state["failed"]) < 300:
                self.state["failed"].append({"instrument_id": inst.id, "reason": reason})

    def _fetch(self, chunk: list[Instrument], start: date | None, end: date) -> dict[str, Dataset]:
        self.requests += 1
        if self.provider.bulk_size > 1:
            return self.provider.daily_bars_bulk(chunk, start, end)
        return {chunk[0].id: self.provider.daily_bars(chunk[0], start, end)}

    def _store(self, inst: Instrument, dataset: Dataset, full_start: date | None, end: date) -> None:
        try:
            bars = self._store_one(inst, dataset, self.mode)
        except NeedsFullHistory:
            # The vendor changed past prices: replace the whole series rather than append to a different adjustment basis.
            self.requests += 1
            bars = self._store_one(inst, self.provider.daily_bars(inst, full_start, end), "full")
            with self._lock:
                self.state["readjusted"] += 1
        with self._lock:
            self.state["stored"] += 1
            self.state["bars"] += bars

    def run(self) -> None:
        self._update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        end = self._today()
        full_start = end.replace(year=end.year - self.years) if self.years else None
        size = max(1, self.provider.bulk_size)
        chunks = [self.instruments[i:i + size] for i in range(0, len(self.instruments), size)]
        outcome, message = "finished", ""
        for index, chunk in enumerate(chunks):
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelled by the user"
                break
            self._update(current=chunk[0].id if len(chunk) == 1 else f"{chunk[0].id} … {chunk[-1].id}")
            start = full_start
            if self.mode == "update":
                known = [self.last_dates[i.id] for i in chunk if i.id in self.last_dates]
                if known:
                    start = min(known) - pd.Timedelta(days=7).to_pytimedelta()
            datasets, error, limited = None, None, False
            for attempt in range(3):
                try:
                    datasets = self._fetch(chunk, start, end)
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
            if limited:  # the allowance is exhausted: stop here; "download missing" resumes from this point later
                outcome, message = "stopped", f"{error}. Start 'Download missing' again later: it resumes where this stopped."
                break
            for inst in chunk:
                if datasets is None:
                    self._fail(inst, error or "request failed")
                elif inst.id not in datasets:
                    self._fail(inst, f"{self.provider.label} has no data for this symbol")
                else:
                    try:
                        self._store(inst, datasets[inst.id], full_start, end)
                    except (ProviderError, ValueError) as exc:
                        self._fail(inst, str(exc))
                    except Exception as exc:  # database or unexpected error: record it, keep the job alive
                        self._fail(inst, f"{type(exc).__name__}: {str(exc)[:200]}")
                with self._lock:
                    self.state["done"] += 1
            if outcome == "failed":
                break
            if index < len(chunks) - 1:
                self._sleep(self.min_interval)
        self._update(status=outcome, message=message, current=None, finished_at=datetime.now(timezone.utc).isoformat())
