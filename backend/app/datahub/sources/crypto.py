"""Public market data of crypto exchanges (Kraken, Binance ...), through the CCXT library the platform already uses.

No API key is ever given to the exchange object, so it cannot reach an account or place an order.
Binance requests go to the exchange's market-data-only host. CCXT paces requests to each venue's limit.

Each venue is its own source: a Binance BTC/USDT bar and a Kraken BTC/USD bar are different instruments
on different books, are stored apart and are only ever compared, never blended.

Limits worth knowing: Kraken's OHLC endpoint only serves the most recent 720 candles of an interval, so
deep intraday history from Kraken has to be rebuilt from its trade history; Binance serves candles back
to a pair's listing. Order books are snapshots at the moment of the request: there is no free history.
"""
from __future__ import annotations

import time

import pandas as pd

from ...market_data.timeframes import OHLCV, tf_delta
from ..http import RequestStats, SourceError, SourceNotFound, SourceRateLimited
from ..lake import now_utc

BINANCE_DATA_URL = "https://data-api.binance.vision/api/v3"
PAGE = {"binance": 1000, "kraken": 720}
VENUES = ("kraken", "binance")


class CryptoVenue:
    def __init__(self, exchange_id: str, exchange=None, binance_url: str = BINANCE_DATA_URL, max_pages: int = 2000) -> None:
        self.name = exchange_id
        self.max_pages = max_pages
        self.stats = RequestStats()
        if exchange is None:
            import ccxt.async_support as ccxt

            if not hasattr(ccxt, exchange_id):
                raise SourceError(f"Unknown exchange {exchange_id}")
            # Spot markets only: nothing here should touch a derivatives or account endpoint.
            exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True, "options": {"defaultType": "spot", "fetchMarkets": ["spot"]}})
            if exchange_id == "binance" and binance_url:
                exchange.urls["api"]["public"] = binance_url.rstrip("/")
        if getattr(exchange, "apiKey", None) or getattr(exchange, "secret", None):
            raise SourceError("The data platform only uses keyless exchange connections")
        self.exchange = exchange

    async def close(self) -> None:
        close = getattr(self.exchange, "close", None)
        if close is not None:
            await close()

    async def _call(self, method: str, *args, **kwargs):
        import ccxt

        started = time.monotonic()
        self.stats.add("requests")
        try:
            out = await getattr(self.exchange, method)(*args, **kwargs)
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            self.stats.add("errors")
            self.stats.add("rate_limited")
            raise SourceRateLimited(f"{self.name}: too many requests") from None
        except ccxt.BadSymbol:
            self.stats.add("errors")
            raise SourceNotFound(f"{self.name} does not list this symbol") from None
        except ccxt.BaseError as exc:
            self.stats.add("errors")
            raise SourceError(f"{self.name}: {type(exc).__name__}") from None
        self.stats.timed((time.monotonic() - started) * 1000)
        return out

    async def bars(self, symbol: str, timeframe: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> tuple[pd.DataFrame, dict]:
        """Completed bars with open time in [start, end]. Returns (rows for the `bars` dataset, notes)."""
        delta = tf_delta(timeframe)
        step_ms = int(delta.total_seconds() * 1000)
        limit = PAGE.get(self.name, 500)
        end = end or now_utc()
        since = int(start.timestamp() * 1000) if start is not None else None
        rows: list[list] = []
        notes: dict = {"pages": 0}
        for _ in range(self.max_pages):
            page = await self._call("fetch_ohlcv", symbol, timeframe, since=since, limit=limit)
            notes["pages"] += 1
            if not page:
                break
            if since is not None and not rows and page[0][0] > since + limit * step_ms:
                # The venue ignored the start and answered with its most recent candles: older history is not served.
                notes["history_truncated_at"] = pd.Timestamp(page[0][0], unit="ms", tz="UTC").isoformat()
            fresh = [r for r in page if not rows or r[0] > rows[-1][0]]
            rows.extend(fresh)
            last = page[-1][0]
            if since is None or not fresh or last >= int(end.timestamp() * 1000) or len(page) < limit:
                break
            since = last + step_ms
        received = now_utc()
        frame = pd.DataFrame([r[:6] for r in rows], columns=["ts", *OHLCV])
        frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True).dt.as_unit("us")
        frame[OHLCV] = frame[OHLCV].astype(float)
        if start is not None:
            frame = frame[frame["ts"] >= start]
        forming = frame["ts"] + delta > received  # a bar still being built is not data yet
        notes["forming_dropped"] = int(forming.sum())
        frame = frame[~forming & (frame["ts"] <= end)].copy()
        frame["source"], frame["venue"], frame["symbol"], frame["timeframe"], frame["received_at"] = self.name, self.name, symbol, timeframe, received
        return frame.reset_index(drop=True), notes

    async def trades(self, symbol: str, since: pd.Timestamp, until: pd.Timestamp | None = None, max_rows: int = 200_000) -> tuple[pd.DataFrame, dict]:
        """Trades after `since`, oldest first. Stops at `until`, at the present or after `max_rows`."""
        until = until or now_utc()
        cursor, until_ms, hour = int(since.timestamp() * 1000), int(until.timestamp() * 1000), 3_600_000
        windowed = self.name == "binance"  # answers at most one hour of trades per request
        seen: dict[str, tuple] = {}
        notes: dict = {"pages": 0}
        for _ in range(self.max_pages):
            page = await self._call("fetch_trades", symbol, since=cursor, limit=1000) or []
            notes["pages"] += 1
            fresh = 0
            for t in page:
                if t.get("timestamp") is None or t.get("price") is None or t["timestamp"] > until_ms:
                    continue
                key = str(t.get("id") or f"{t['timestamp']}-{t['price']}-{t.get('amount')}")
                if key not in seen:
                    seen[key] = (t["timestamp"], key, float(t["price"]), float(t.get("amount") or 0), t.get("side") or "")
                    fresh += 1
            newest = max((t["timestamp"] for t in page if t.get("timestamp") is not None), default=None)
            if len(seen) >= max_rows:
                notes["stopped_at_row_limit"] = True
                break
            if newest is not None and newest >= until_ms:
                break
            if len(page) >= 1000 and newest is not None:
                cursor = newest + (0 if fresh else 1)  # several trades can share a millisecond: ask for it again, duplicates are dropped
            elif windowed and cursor + hour < until_ms:
                cursor += hour
            else:
                break
        received = now_utc()
        frame = pd.DataFrame(list(seen.values()), columns=["ts", "trade_id", "price", "amount", "side"])
        frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True).dt.as_unit("us")
        frame["source"], frame["venue"], frame["symbol"], frame["received_at"] = self.name, self.name, symbol, received
        return frame.sort_values(["ts", "trade_id"]).reset_index(drop=True), notes

    async def order_book(self, symbol: str, depth: int = 20) -> pd.DataFrame:
        """One snapshot of the top `depth` levels per side (a row for the `orderbook` dataset)."""
        book = await self._call("fetch_order_book", symbol, depth)
        received = now_utc()
        bids, asks = (book.get("bids") or [])[:depth], (book.get("asks") or [])[:depth]
        if not bids or not asks:
            raise SourceError(f"{self.name} returned an empty order book")
        stamp = book.get("timestamp")
        ts = pd.Timestamp(stamp, unit="ms", tz="UTC").as_unit("us") if stamp else received
        return pd.DataFrame([{
            "ts": ts, "ts_origin": "exchange" if stamp else "received", "best_bid": float(bids[0][0]), "best_ask": float(asks[0][0]),
            "bid_px": [float(l[0]) for l in bids], "bid_sz": [float(l[1]) for l in bids],
            "ask_px": [float(l[0]) for l in asks], "ask_sz": [float(l[1]) for l in asks], "levels": min(len(bids), len(asks)),
            "source": self.name, "venue": self.name, "symbol": symbol, "received_at": received}])
