"""Alpaca's official OHLCV bars at intraday resolution, for many symbols per request.

Three feeds, never mixed without a label (the label is stored on every bar as its `source`):

    alpaca:iex          US stocks and ETFs, IEX exchange only. Real time on the free plan. Prices follow the market
                        closely; volume is only what traded on IEX, a small part of the total.
    alpaca:sip-delayed  US stocks and ETFs, consolidated tape (every exchange). The free plan may only read data
                        older than 15 minutes, so this feed is for history, never for the current minute.
    alpaca:crypto       Crypto pairs on Alpaca's own venue. Real time; works without a key at a slow rate.

Stock bars are split-adjusted and limited to the regular session by the caller. The API key travels in request
headers only and never appears in an error message or a log line.

VERIFICATION STATUS: request parameters follow the same endpoints the platform already uses for daily bars and for
the recent-minutes backfill; behaviour is tested against emulated answers. Not exercised with real keys from the
development environment, which has no route to Alpaca.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import httpx
import pandas as pd

from .timeframes import OHLCV, tf_delta

BASE_URL = "https://data.alpaca.markets"
TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour"}
SOURCES = {"iex": "alpaca:iex", "sip": "alpaca:sip-delayed", "crypto": "alpaca:crypto"}
SIP_DELAY = pd.Timedelta(minutes=16)
PAGE = 10_000
# What a stored `source` label means, for the dashboard. Labels written by older versions are included.
FEED_LABELS = {
    "alpaca:iex": "IEX (one US exchange), official bars",
    "alpaca:sip-delayed": "Consolidated tape (all US exchanges), delayed 15 minutes",
    "alpaca:crypto": "Alpaca crypto, official bars",
    "live:alpaca-stocks": "Rebuilt from live IEX trades and quotes",
    "live:alpaca-crypto": "Rebuilt from live crypto trades and quotes",
    "alpaca": "Consolidated tape, daily, adjusted",
    "alpaca-iex": "IEX, daily",
    "alpaca-crypto": "Alpaca crypto, daily",
}


class AlpacaBarsError(Exception):
    """A message that is safe to show: it never contains a key."""


class AlpacaAuthError(AlpacaBarsError):
    pass


class FeedNotAllowed(AlpacaBarsError):
    """The plan of this key does not include the requested feed or period."""


def describe_source(source: str) -> str:
    return "; ".join(FEED_LABELS.get(part, part) for part in source.split("+")) if source else ""


class AlpacaBars:
    def __init__(self, key: str = "", secret: str = "", base_url: str = BASE_URL, client: httpx.Client | None = None,
                 min_interval: float = 0.35, max_retries: int = 4, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._key, self._secret = key, secret
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self.min_interval, self.max_retries, self._sleep, self.clock = min_interval, max_retries, sleep, clock
        self._last = 0.0
        self.requests = self.rate_limited = self.retries = 0

    @property
    def has_key(self) -> bool:
        return bool(self._key and self._secret)

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret} if self.has_key else {}

    def _get(self, path: str, params: dict, keyless_ok: bool = False) -> dict:
        """GET with pacing and retries. Rate limits (429) and server errors are retried with a pause; a rejected key is not."""
        headers, last = self._headers(), None
        for attempt in range(self.max_retries + 1):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                r = self.client.get(f"{self.base_url}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:  # the exception text is not passed on: keep the message generic
                last = AlpacaBarsError(f"Alpaca could not be reached ({type(exc).__name__})")
            else:
                code = r.status_code
                if code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        raise AlpacaBarsError("Alpaca returned a response that is not JSON") from None
                message = _message(r)
                if code in (401, 403) and keyless_ok and headers:
                    headers = {}  # crypto data needs no key: a refused (for example regenerated) key must not stop it
                    continue
                if code == 401:
                    raise AlpacaAuthError("Alpaca rejected the API key")
                if code == 403:
                    raise FeedNotAllowed(f"Alpaca refused the request: {message or 'not included in this plan'}")
                if code == 429:
                    self.rate_limited += 1
                    pause = _retry_after(r) or min(60.0, 3.0 * 2 ** attempt)
                    last = AlpacaBarsError("Alpaca rate limit reached")
                    if attempt < self.max_retries:
                        self.retries += 1
                        self._sleep(pause)
                    continue
                if code < 500:
                    raise AlpacaBarsError(f"Alpaca refused the request (HTTP {code}): {message}")
                last = AlpacaBarsError(f"Alpaca answered with HTTP {code}")
            if attempt < self.max_retries:
                self.retries += 1
                self._sleep(min(30.0, 1.0 * 2 ** attempt))
        assert last is not None
        raise last

    def pages(self, symbols: list[str], timeframe: str, start: pd.Timestamp, end: pd.Timestamp | None = None, feed: str = "iex",
              newest_first: bool = False, max_pages: int = 5000) -> Iterator[dict[str, pd.DataFrame]]:
        """Completed bars with open time in [start, end], one dictionary {symbol: bars} per page of 10,000 bars.

        `feed` is "iex", "sip" or "crypto". For "sip" the period is cut 16 minutes before now (the free plan's limit).
        A bar that is still forming is never returned. With `newest_first` the pages run backwards in time."""
        if timeframe not in TIMEFRAMES:
            raise AlpacaBarsError(f"Alpaca bars are not requested at the {timeframe} timeframe")
        now = pd.Timestamp(self.clock())
        delta = tf_delta(timeframe)
        end = min(end, now) if end is not None else now
        if feed == "sip":
            end = min(end, now - SIP_DELAY)
        if not symbols or end < start:
            return
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        params = {"symbols": ",".join(symbols), "timeframe": TIMEFRAMES[timeframe], "start": start.strftime(fmt), "end": end.strftime(fmt),
                  "limit": PAGE, "sort": "desc" if newest_first else "asc"}
        if feed == "crypto":
            path = "/v1beta3/crypto/us/bars"
        else:
            path = "/v2/stocks/bars"
            params.update(feed=feed, adjustment="split")
        for _ in range(max_pages):  # bounded, so a faulty page token cannot loop forever
            body = self._get(path, params, keyless_ok=feed == "crypto")
            if not isinstance(body, dict) or not isinstance(body.get("bars") or {}, dict):
                raise AlpacaBarsError("Alpaca returned an unexpected response")
            page = {}
            for symbol, rows in (body.get("bars") or {}).items():
                frame = _frame(rows or [])
                frame = frame[frame.index + delta <= now]  # the bar being built is not data yet
                if len(frame):
                    page[symbol] = frame
            if page:
                yield page
            token = body.get("next_page_token")
            if not token:
                return
            params = {**params, "page_token": token}

    def most_active(self, top: int = 100, by: str = "trades") -> list[str]:
        """Alpaca's screener: the most traded US stocks of the latest session, by number of trades or by volume."""
        body = self._get("/v1beta1/screener/stocks/most-actives", {"by": by, "top": min(top, 100)})
        return [row["symbol"] for row in body.get("most_actives", []) if str(row.get("symbol", "")).isalpha()]


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("message") or "")[:200] if isinstance(body, dict) else ""
    except ValueError:
        return ""


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, min(float(response.headers.get("retry-after", "")), 120.0))
    except ValueError:
        return None


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Alpaca bar objects -> OHLCV indexed by bar open time (UTC), oldest first. Malformed and non-positive rows are dropped."""
    records = []
    for b in rows:
        try:
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
            if min(o, h, l, c) <= 0:
                continue
            records.append((pd.Timestamp(b["t"]), o, h, l, c, float(b.get("v") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    frame = pd.DataFrame(records, columns=["ts", *OHLCV])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.set_index("ts").sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def in_session(index: pd.DatetimeIndex, calendar) -> pd.Series:
    """Which bar open times fall inside the regular session of `calendar` (all of them for a continuous market)."""
    if calendar.open_time is None or calendar.close_time is None:
        return pd.Series(True, index=index)
    local = index.tz_convert(calendar.tz)
    clock = local.hour * 60 + local.minute
    opens, closes = calendar.open_time.hour * 60 + calendar.open_time.minute, calendar.close_time.hour * 60 + calendar.close_time.minute
    days = pd.Series(local.date, index=index)
    keep = (clock >= opens) & (clock < closes) & local.weekday.isin(list(calendar.weekdays)) & ~days.isin(list(calendar.holidays)).to_numpy()
    return pd.Series(keep, index=index)
