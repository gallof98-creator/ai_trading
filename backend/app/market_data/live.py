"""Live market data: ticks from streaming providers, one-minute bars built from them, fan-out to the dashboard.

Every tick carries its provider, the provider's own timestamp, the time it was received and the class of
feed it came from (LIVE, DELAYED, END_OF_DAY). A delayed feed is never relabelled as live, and a live feed
whose last tick is old is reported as stale so that nothing is sized or approved on an outdated price.

Sources are pluggable (`LiveSource`). A source that cannot hold a streaming connection falls back to
polling the provider's official REST endpoints and says so in its status.
"""
from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable

import httpx
import pandas as pd

from ..core.logging import get_logger

log = get_logger(__name__)


class FeedStatus(StrEnum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    END_OF_DAY = "END_OF_DAY"


@dataclass(frozen=True)
class Tick:
    instrument_id: str
    price: float  # trade price, or bid/ask midpoint for a quote
    ts_source: datetime  # the provider's timestamp for the event
    ts_received: datetime
    provider: str
    status: FeedStatus
    kind: str = "trade"  # trade | quote
    size: float = 0.0
    bid: float | None = None
    ask: float | None = None
    venue: str = ""  # which market the feed covers, e.g. "IEX" (one US exchange, not the consolidated tape)

    @property
    def latency_ms(self) -> float:
        return (self.ts_received - self.ts_source).total_seconds() * 1000


@dataclass
class _Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 0

    def to_dict(self) -> dict:
        return {"t": int(self.start.timestamp()), "o": self.open, "h": self.high, "l": self.low, "c": self.close, "v": self.volume}


@dataclass
class _ProviderStats:
    state: str = "stopped"  # stopped | connecting | streaming | polling | error
    transport: str = ""
    detail: str = ""
    ticks: int = 0
    last_tick: datetime | None = None
    last_latency_ms: float | None = None
    errors: int = 0
    last_error: str = ""
    last_error_at: datetime | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=5000))  # (received time, latency ms)


class LiveHub:
    """Thread-safe meeting point between sources (producers) and the engine and dashboard (consumers)."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 on_bar: Callable[[str, pd.DataFrame, str], None] | None = None, stale_after: timedelta = timedelta(seconds=60)) -> None:
        self.clock, self.on_bar, self.stale_after = clock, on_bar, stale_after
        self._lock = threading.Lock()
        self.latest: dict[str, Tick] = {}
        self.last_trade: dict[str, Tick] = {}
        self._bars: dict[str, _Bar] = {}
        self._subscribers: dict[int, tuple[Callable[[dict], None], set[str] | None]] = {}
        self._next_sub = 0
        self.providers: dict[str, _ProviderStats] = {}
        self.out_of_order = 0

    # ---- producers
    def set_state(self, provider: str, state: str, transport: str = "", detail: str = "") -> None:
        with self._lock:
            s = self.providers.setdefault(provider, _ProviderStats())
            s.state, s.detail = state, detail
            if transport:
                s.transport = transport

    def record_error(self, provider: str, message: str) -> None:
        with self._lock:
            s = self.providers.setdefault(provider, _ProviderStats())
            s.errors += 1
            s.last_error, s.last_error_at = message[:300], self.clock()

    def publish(self, tick: Tick) -> None:
        completed: _Bar | None = None
        with self._lock:
            s = self.providers.setdefault(tick.provider, _ProviderStats())
            s.ticks += 1
            s.last_tick, s.last_latency_ms = tick.ts_received, tick.latency_ms
            s.recent.append((tick.ts_received, tick.latency_ms))
            previous = self.latest.get(tick.instrument_id)
            if previous is None or tick.ts_source >= previous.ts_source:
                self.latest[tick.instrument_id] = tick
            if tick.kind == "trade":
                self.last_trade[tick.instrument_id] = tick
            minute = tick.ts_source.replace(second=0, microsecond=0)
            bar = self._bars.get(tick.instrument_id)
            if bar is not None and minute < bar.start:
                self.out_of_order += 1  # a late tick never rewrites a bar that has already moved on
            else:
                if bar is None or minute > bar.start:
                    completed = bar
                    bar = self._bars[tick.instrument_id] = _Bar(minute, tick.price, tick.price, tick.price, tick.price)
                bar.high, bar.low, bar.close = max(bar.high, tick.price), min(bar.low, tick.price), tick.price
                bar.ticks += 1
                if tick.kind == "trade":
                    bar.volume += tick.size
            event = {"type": "tick", "instrument_id": tick.instrument_id, "price": tick.price, "kind": tick.kind, "size": tick.size, "bid": tick.bid,
                     "ask": tick.ask, "ts_source": tick.ts_source.isoformat(), "ts_received": tick.ts_received.isoformat(),
                     "latency_ms": round(tick.latency_ms, 1), "provider": tick.provider, "status": tick.status.value, "venue": tick.venue,
                     "bar": self._bars[tick.instrument_id].to_dict()}
            targets = [send for send, symbols in self._subscribers.values() if symbols is None or tick.instrument_id in symbols]
        for send in targets:
            try:
                send(event)
            except Exception:  # a slow or closed dashboard connection must never stall the feed
                pass
        if completed is not None and self.on_bar is not None:
            frame = pd.DataFrame([[completed.open, completed.high, completed.low, completed.close, completed.volume]],
                                 index=pd.DatetimeIndex([pd.Timestamp(completed.start)], name="ts"), columns=["open", "high", "low", "close", "volume"])
            try:
                self.on_bar(tick.instrument_id, frame, tick.provider)
            except Exception as exc:
                self.record_error(tick.provider, f"bar not stored: {exc}")

    # ---- consumers
    def subscribe(self, send: Callable[[dict], None], symbols: set[str] | None = None) -> int:
        with self._lock:
            self._next_sub += 1
            self._subscribers[self._next_sub] = (send, symbols)
            return self._next_sub

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)

    def quote(self, instrument_id: str) -> dict | None:
        """Latest known price with its age. `fresh` is False when the last tick is older than the staleness limit."""
        with self._lock:
            tick = self.latest.get(instrument_id)
            if tick is None:
                return None
            age = (self.clock() - tick.ts_source).total_seconds()
            return {"instrument_id": instrument_id, "price": tick.price, "bid": tick.bid, "ask": tick.ask, "kind": tick.kind,
                    "ts_source": tick.ts_source.isoformat(), "ts_received": tick.ts_received.isoformat(), "age_seconds": round(age, 1),
                    "provider": tick.provider, "status": tick.status.value, "venue": tick.venue,
                    "fresh": tick.status is FeedStatus.LIVE and age <= self.stale_after.total_seconds(),
                    "spread": (tick.ask - tick.bid) if tick.bid and tick.ask else None}

    def status(self) -> dict:
        now = self.clock()
        with self._lock:
            out = {}
            for name, s in self.providers.items():
                window = [(t, lat) for t, lat in s.recent if (now - t).total_seconds() <= 60]
                lat = sorted(l for _, l in window)
                out[name] = {"state": s.state, "transport": s.transport, "detail": s.detail, "ticks_total": s.ticks, "ticks_last_minute": len(window),
                             "last_tick": s.last_tick.isoformat() if s.last_tick else None,
                             "seconds_since_last_tick": round((now - s.last_tick).total_seconds(), 1) if s.last_tick else None,
                             "median_latency_ms": round(lat[len(lat) // 2], 1) if lat else None, "max_latency_ms": round(lat[-1], 1) if lat else None,
                             "errors": s.errors,
                             # A problem that has not recurred for two minutes is history, not the current state.
                             "last_error": s.last_error if s.last_error_at and (now - s.last_error_at).total_seconds() <= 120 else ""}
            return {"providers": out, "instruments": len(self.latest), "subscribers": len(self._subscribers), "out_of_order_ticks": self.out_of_order,
                    "stale_after_seconds": self.stale_after.total_seconds()}


# ---------------------------------------------------------------- sources
def _ts(text: str) -> datetime:
    return pd.Timestamp(text).tz_convert("UTC").to_pydatetime() if pd.Timestamp(text).tzinfo else pd.Timestamp(text, tz="UTC").to_pydatetime()


class LiveSource(ABC):
    """One provider connection. `symbols` maps the provider's symbol to the platform instrument id."""

    name: str = ""

    def __init__(self, hub: LiveHub) -> None:
        self.hub = hub
        self.symbols: dict[str, str] = {}
        self._changed = threading.Event()

    def set_symbols(self, symbols: dict[str, str]) -> None:
        self.symbols = dict(symbols)
        self._changed.set()

    @abstractmethod
    def run(self, stop: threading.Event) -> None:
        """Blocks until `stop` is set. Must survive disconnections."""


class AlpacaLive(LiveSource):
    """Alpaca market data. Streams over the official WebSocket; if that cannot be opened (for example a network
    that blocks WebSocket upgrades) it polls the official REST "latest" endpoints instead.

    `asset` is "crypto" (live, no key needed for REST) or "stocks" (IEX feed on the free plan: live, but one
    exchange only, not the consolidated tape; needs a key).
    """

    DATA = "https://data.alpaca.markets"
    STREAM = "wss://stream.data.alpaca.markets"

    def __init__(self, hub: LiveHub, asset: str, key: str = "", secret: str = "", feed: str = "iex", data_url: str | None = None,
                 stream_url: str | None = None, poll_seconds: float | None = None, allow_stream: bool = True, client: httpx.Client | None = None) -> None:
        super().__init__(hub)
        self.asset, self.key, self.secret, self.feed = asset, key, secret, feed
        self.name = f"alpaca-{asset}"
        self.venue = "Alpaca crypto" if asset == "crypto" else feed.upper()
        self.data_url, self.stream_url = (data_url or self.DATA).rstrip("/"), (stream_url or self.STREAM).rstrip("/")
        # Without a key Alpaca allows only a few requests a minute; with one, 200.
        self.poll_seconds = poll_seconds if poll_seconds is not None else (1.5 if key else 5.0)
        self.allow_stream = allow_stream and bool(key)
        self.client = client or httpx.Client(timeout=10)
        self._seen: dict[tuple[str, str], str] = {}

    # -- shared parsing
    def _trade(self, symbol: str, price: float, size: float, ts: str) -> None:
        iid = self.symbols.get(symbol)
        if iid and price > 0:
            self.hub.publish(Tick(iid, float(price), _ts(ts), self.hub.clock(), self.name, FeedStatus.LIVE, "trade", float(size or 0), venue=self.venue))

    def _quote(self, symbol: str, bid: float, ask: float, ts: str) -> None:
        iid = self.symbols.get(symbol)
        if iid and bid and ask and bid > 0 and ask >= bid:
            self.hub.publish(Tick(iid, (bid + ask) / 2, _ts(ts), self.hub.clock(), self.name, FeedStatus.LIVE, "quote", bid=float(bid), ask=float(ask), venue=self.venue))

    def recent_bars(self, symbol: str, limit: int = 1000) -> pd.DataFrame:
        """The latest one-minute bars from Alpaca's REST history, so a chart is not empty while the first live minute forms."""
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret} if self.key else {}
        start = (self.hub.clock() - timedelta(minutes=limit * (1 if self.asset == "crypto" else 4))).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"symbols": symbol, "timeframe": "1Min", "limit": limit, "start": start, "sort": "desc"} | ({} if self.asset == "crypto" else {"feed": self.feed})
        url = f"{self.data_url}/v1beta3/crypto/us/bars" if self.asset == "crypto" else f"{self.data_url}/v2/stocks/bars"
        r = self.client.get(url, params=params, headers=headers)
        r.raise_for_status()
        rows = (r.json().get("bars") or {}).get(symbol) or []
        frame = pd.DataFrame([[b["o"], b["h"], b["l"], b["c"], b["v"]] for b in rows], columns=["open", "high", "low", "close", "volume"],
                             index=pd.DatetimeIndex([pd.Timestamp(b["t"]) for b in rows], name="ts"))
        return frame.sort_index()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.symbols:
                self.hub.set_state(self.name, "stopped", detail="no instruments subscribed")
                self._changed.wait(1.0)
                self._changed.clear()
                continue
            if self.allow_stream:
                try:
                    self._stream(stop)
                    continue
                except Exception as exc:
                    self.hub.record_error(self.name, f"stream: {type(exc).__name__}: {exc}")
                    self.allow_stream = False  # do not hammer a connection that cannot be opened; polling takes over
            self._poll(stop)

    # -- websocket
    def _stream(self, stop: threading.Event) -> None:
        from websockets.sync.client import connect

        path = "/v1beta3/crypto/us" if self.asset == "crypto" else f"/v2/{self.feed}"
        self.hub.set_state(self.name, "connecting", "websocket")
        with connect(self.stream_url + path, open_timeout=10, close_timeout=2) as ws:
            ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
            subscribed: list[str] = []
            self._changed.set()
            while not stop.is_set():
                if self._changed.is_set():
                    self._changed.clear()
                    wanted = sorted(self.symbols)
                    gone = [s for s in subscribed if s not in wanted]
                    if gone:
                        ws.send(json.dumps({"action": "unsubscribe", "trades": gone, "quotes": gone}))
                    if wanted:
                        ws.send(json.dumps({"action": "subscribe", "trades": wanted, "quotes": wanted}))
                    subscribed = wanted
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                for m in json.loads(raw):
                    kind = m.get("T")
                    if kind == "t":
                        self._trade(m["S"], m["p"], m.get("s", 0), m["t"])
                    elif kind == "q":
                        self._quote(m["S"], m.get("bp"), m.get("ap"), m["t"])
                    elif kind == "error":
                        raise ConnectionError(f"{m.get('code')}: {m.get('msg')}")
                    elif kind == "success" and m.get("msg") == "authenticated":
                        self.hub.set_state(self.name, "streaming", "websocket", f"push stream, {self.venue}")

    # -- REST polling
    def _poll(self, stop: threading.Event) -> None:
        self.hub.set_state(self.name, "polling", f"REST requests every {self.poll_seconds / 2:g}s",
                           "no streaming connection available; latest trades and quotes are fetched repeatedly" + ("" if self.key else " (no API key: slow rate)"))
        base = f"{self.data_url}/v1beta3/crypto/us/latest" if self.asset == "crypto" else f"{self.data_url}/v2/stocks"
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret} if self.key else {}
        turn = 0
        self._changed.clear()
        while not stop.is_set() and not self._changed.is_set():
            what = ("quotes", "trades")[turn % 2]
            turn += 1
            url = f"{base}/{what}" if self.asset == "crypto" else f"{base}/{what}/latest"
            params = {"symbols": ",".join(sorted(self.symbols))} | ({} if self.asset == "crypto" else {"feed": self.feed})
            try:
                r = self.client.get(url, params=params, headers=headers)
                if r.status_code == 429:
                    self.hub.record_error(self.name, "rate limited by Alpaca; slowing down")
                    stop.wait(self.poll_seconds * 3)
                    continue
                if r.status_code in (401, 403):
                    self.hub.set_state(self.name, "error", detail="Alpaca rejected the API key")
                    self.hub.record_error(self.name, f"HTTP {r.status_code}")
                    stop.wait(30)
                    continue
                r.raise_for_status()
                for symbol, item in (r.json().get(what) or {}).items():
                    if self._seen.get((what, symbol)) == item["t"]:
                        continue  # nothing new since the last poll
                    self._seen[(what, symbol)] = item["t"]
                    if what == "trades":
                        self._trade(symbol, item["p"], item.get("s", 0), item["t"])
                    else:
                        self._quote(symbol, item.get("bp"), item.get("ap"), item["t"])
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.hub.record_error(self.name, f"poll: {type(exc).__name__}: {str(exc)[:200]}")
            stop.wait(self.poll_seconds / 2)


class FinnhubLive(LiveSource):
    """Finnhub trade stream (official WebSocket). US trades are live on the free plan; there is no REST fallback
    that would be honest to call live, so without a streaming connection this source reports an error."""

    name = "finnhub"
    STREAM = "wss://ws.finnhub.io"

    def __init__(self, hub: LiveHub, key: str, stream_url: str | None = None) -> None:
        super().__init__(hub)
        self.key, self.stream_url = key, (stream_url or self.STREAM).rstrip("/")

    def run(self, stop: threading.Event) -> None:
        from websockets.sync.client import connect

        backoff = 2.0
        while not stop.is_set():
            if not self.symbols:
                self.hub.set_state(self.name, "stopped", detail="no instruments subscribed")
                self._changed.wait(1.0)
                self._changed.clear()
                continue
            try:
                self.hub.set_state(self.name, "connecting", "websocket")
                with connect(f"{self.stream_url}?token={self.key}", open_timeout=10, close_timeout=2) as ws:
                    for symbol in sorted(self.symbols):
                        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                    self._changed.clear()
                    self.hub.set_state(self.name, "streaming", "websocket", "push stream of trades")
                    backoff = 2.0
                    while not stop.is_set() and not self._changed.is_set():
                        try:
                            m = json.loads(ws.recv(timeout=1.0))
                        except TimeoutError:
                            continue
                        if m.get("type") == "trade":
                            for d in m.get("data", []):
                                iid = self.symbols.get(d.get("s"))
                                if iid and d.get("p"):
                                    ts = datetime.fromtimestamp(d["t"] / 1000, tz=timezone.utc)
                                    self.hub.publish(Tick(iid, float(d["p"]), ts, self.hub.clock(), self.name, FeedStatus.LIVE, "trade", float(d.get("v") or 0)))
                        elif m.get("type") == "error":
                            raise ConnectionError(str(m.get("msg")))
            except Exception as exc:
                # The key travels in the URL: never let it reach a log or a status message.
                text = str(exc).replace(self.key, "***") if self.key else str(exc)
                self.hub.record_error(self.name, f"{type(exc).__name__}: {text}")
                self.hub.set_state(self.name, "error", "websocket", "streaming connection failed; retrying")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)


class LiveFeed:
    """Owns the sources and their threads. Sources are added by name; subscribing routes each instrument to the
    first source that can serve it."""

    def __init__(self, hub: LiveHub) -> None:
        self.hub = hub
        self.sources: dict[str, LiveSource] = {}
        self.routes: dict[str, Callable[[object], str | None]] = {}  # source name -> instrument -> provider symbol or None
        self._threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.subscribed: dict[str, str] = {}  # instrument id -> source name
        self._instruments: dict[str, object] = {}
        self._lock = threading.RLock()

    def add_source(self, source: LiveSource, route: Callable[[object], str | None]) -> None:
        with self._lock:
            self.remove_source(source.name)
            self.sources[source.name], self.routes[source.name] = source, route

    def remove_source(self, name: str) -> None:
        pair = self._threads.pop(name, None)
        if pair:
            pair[1].set()
        self.sources.pop(name, None)
        self.routes.pop(name, None)
        self.subscribed = {i: s for i, s in self.subscribed.items() if s != name}

    def subscribe(self, instruments: list) -> dict:
        """Returns {"subscribed": {...}, "unavailable": {instrument: reason}}."""
        out: dict = {"subscribed": {}, "unavailable": {}}
        with self._lock:
            for inst in instruments:
                self._instruments[inst.id] = inst
                for name, route in self.routes.items():
                    symbol = route(inst)
                    if symbol:
                        self.subscribed[inst.id] = name
                        out["subscribed"][inst.id] = name
                        break
                else:
                    out["unavailable"][inst.id] = "no connected live source covers this instrument"
            self._apply()
        return out

    def unsubscribe(self, instrument_ids: list[str]) -> None:
        with self._lock:
            for iid in instrument_ids:
                self.subscribed.pop(iid, None)
            self._apply()

    def _apply(self) -> None:
        by_source: dict[str, dict[str, str]] = {name: {} for name in self.sources}
        for iid, name in self.subscribed.items():
            if name in by_source and iid in self._instruments:
                by_source[name][self.routes[name](self._instruments[iid])] = iid
        for name, source in self.sources.items():
            source.set_symbols(by_source[name])
            if by_source[name] and name not in self._threads:
                stop = threading.Event()
                thread = threading.Thread(target=source.run, args=(stop,), name=f"live-{name}", daemon=True)
                self._threads[name] = (thread, stop)
                thread.start()

    def stop(self) -> None:
        with self._lock:
            for _, stop in self._threads.values():
                stop.set()
            self._threads.clear()


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
