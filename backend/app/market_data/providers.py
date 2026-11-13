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

    VERIFICATION STATUS: written from the vendor's documented endpoints and tested against
    recorded-shape responses only. Use the dashboard's "Test connection" before relying on it.
    """

    name = "eodhd"
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


class DownloadJob:
    """Background download of daily history for many instruments, one request at a time.

    Each instrument is stored in its own transaction; a failure is recorded and the job moves on.
    An authentication failure stops the job (every further request would fail the same way).
    """

    def __init__(self, provider: PriceProvider, instruments: list[Instrument], store_one: Callable[[Instrument, Dataset], int],
                 years: int = 10, min_interval: float = 0.1, sleep: Callable[[float], None] = time.sleep,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.provider, self.instruments, self._store_one = provider, instruments, store_one
        self.years, self.min_interval, self._sleep, self._today = years, min_interval, sleep, today
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
        for inst in self.instruments:
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelled by the user"
                break
            self._update(current=inst.id)
            error = None
            for attempt in range(3):
                try:
                    bars = self._store_one(inst, self.provider.daily_bars(inst, start, end))
                    with self._lock:
                        self.state["stored"] += 1
                        self.state["bars"] += bars
                    error = None
                    break
                except ProviderRateLimited as exc:
                    error = str(exc)
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
                self.state["done"] += 1
                if error:
                    self.state["failed_count"] += 1
                    if len(self.state["failed"]) < 300:
                        self.state["failed"].append({"instrument_id": inst.id, "reason": error})
            if outcome == "failed":
                break
            if error and "request limit" in error:
                outcome, message = "failed", error
                break
            self._sleep(self.min_interval)
        self._update(status=outcome, message=message, current=None, finished_at=datetime.now(timezone.utc).isoformat())
