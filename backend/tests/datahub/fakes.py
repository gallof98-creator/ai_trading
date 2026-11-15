"""Stand-ins for the data providers. Responses have the shapes the providers document; nothing here touches the network."""
from __future__ import annotations

import httpx

MINUTE = 60_000
T0 = 1_700_000_040_000  # 2023-11-14 22:14:00 UTC, on the minute grid

CPI = [  # January 2024 is first published on 13 February and revised on 12 March, when February is first published
    {"realtime_start": "2023-02-14", "realtime_end": "9999-12-31", "date": "2023-01-01", "value": "299.170"},
    {"realtime_start": "2023-03-14", "realtime_end": "9999-12-31", "date": "2023-02-01", "value": "300.840"},
    {"realtime_start": "2024-02-13", "realtime_end": "2024-03-11", "date": "2024-01-01", "value": "308.417"},
    {"realtime_start": "2024-03-12", "realtime_end": "9999-12-31", "date": "2024-01-01", "value": "309.685"},
    {"realtime_start": "2024-03-12", "realtime_end": "9999-12-31", "date": "2024-02-01", "value": "310.326"},
]
YIELD_10Y = [{"realtime_start": "2026-11-14", "realtime_end": "2026-11-14", "date": d, "value": v}
             for d, v in (("2024-03-08", "4.09"), ("2024-03-11", "4.10"), ("2024-03-12", "."), ("2024-03-13", "4.19"))]
YIELD_2Y = [{"realtime_start": "2026-11-14", "realtime_end": "2026-11-14", "date": d, "value": v}
            for d, v in (("2024-03-08", "4.48"), ("2024-03-11", "4.51"), ("2024-03-13", "4.61"))]


class Fred:
    """FRED's series/observations endpoint. Records every request; can refuse vintage requests like the real API does for daily series."""

    def __init__(self, key: str = "k" * 32) -> None:
        self.key, self.calls, self.refuse_vintages = key, [], set()
        self.series = {"CPIAUCSL": CPI, "DGS10": YIELD_10Y, "DGS2": YIELD_2Y}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.calls.append(q)
        if q.get("api_key") != self.key:
            return httpx.Response(400, json={"error_code": 400, "error_message": "Bad Request. The value for variable api_key is not registered."})
        series = q["series_id"]
        if series not in self.series:
            return httpx.Response(400, json={"error_code": 400, "error_message": "Bad Request. The series does not exist."})
        if "realtime_start" in q and series in self.refuse_vintages:
            return httpx.Response(400, json={"error_code": 400, "error_message": "Bad Request. Too many vintage dates."})
        rows = self.series[series]
        if "realtime_start" not in q:  # the default real-time period is today: only the current figures
            latest = {}
            for r in rows:
                latest[r["date"]] = r
            rows = list(latest.values())
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 100000))
        return httpx.Response(200, json={"count": len(rows), "offset": offset, "limit": limit, "observations": rows[offset:offset + limit]})


SEC_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}
SEC_SUBMISSIONS = {"cik": "320193", "name": "Apple Inc.", "tickers": ["AAPL"], "filings": {"recent": {
    "accessionNumber": ["0000320193-24-000123", "0000320193-24-000081", "0000320193-24-000006"],
    "filingDate": ["2024-11-01", "2024-08-02", "2024-02-02"], "reportDate": ["2024-09-28", "2024-06-29", "2023-12-30"],
    "acceptanceDateTime": ["2024-11-01T06:01:36.000Z", "2024-08-01T18:03:35.000Z", "2024-02-01T18:03:22.000Z"],
    "form": ["10-K", "10-Q", "10-Q"], "primaryDocument": ["aapl-20240928.htm", "aapl-20240629.htm", "aapl-20231230.htm"],
    "items": ["", "", ""], "isXBRL": [1, 1, 1]}, "files": []}}
SEC_FACTS = {"cik": 320193, "entityName": "Apple Inc.", "facts": {"us-gaap": {
    "Revenues": {"label": "Revenues", "units": {"USD": [
        {"start": "2023-10-01", "end": "2023-12-30", "val": 119575000000, "accn": "0000320193-24-000006", "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-02-02", "frame": "CY2023Q4"},
        # The same quarter restated in a later filing whose acceptance time is not in the downloaded index
        {"start": "2023-10-01", "end": "2023-12-30", "val": 119600000000, "accn": "0000320193-25-000008", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-01-31"},
        {"start": "2024-03-31", "end": "2024-06-29", "val": 85777000000, "accn": "0000320193-24-000081", "fy": 2024, "fp": "Q3", "form": "10-Q", "filed": "2024-08-02"},
        {"start": "2023-10-01", "end": "2024-09-28", "val": 391035000000, "accn": "0000320193-24-000123", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"}]}},
    "Assets": {"label": "Assets", "units": {"USD": [
        {"end": "2024-09-28", "val": 364980000000, "accn": "0000320193-24-000123", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
        {"end": "2024-06-29", "val": None, "accn": "0000320193-24-000081", "fy": 2024, "fp": "Q3", "form": "10-Q", "filed": "2024-08-02"}]}}}}}


class Sec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((str(request.url), request.headers.get("user-agent", "")))
        if "@" not in request.headers.get("user-agent", ""):
            return httpx.Response(403, text="Undeclared automated tool")
        path = request.url.path
        if path == "/files/company_tickers.json":
            return httpx.Response(200, json=SEC_TICKERS)
        if path == "/submissions/CIK0000320193.json":
            return httpx.Response(200, json=SEC_SUBMISSIONS)
        if path == "/api/xbrl/companyfacts/CIK0000320193.json":
            return httpx.Response(200, json=SEC_FACTS)
        return httpx.Response(404, text="not found")


class Exchange:
    """A CCXT-shaped exchange with `history` one-minute candles from T0. `page` is the most it returns per request;
    with `recent_only` it ignores `since` and answers with its latest candles, as Kraken's OHLC endpoint does."""

    apiKey = None
    secret = None

    def __init__(self, history: int = 2500, page: int = 1000, recent_only: bool = False, base: float = 100.0) -> None:
        self.history, self.page, self.recent_only, self.base = history, page, recent_only, base
        self.calls: list[tuple] = []
        self.closed = False
        self.bad_candle_at: int | None = None

    def _candle(self, i: int) -> list:
        price = self.base + i * 0.01
        row = [T0 + i * MINUTE, price, price + 0.5, price - 0.5, price + 0.1, 5.0 + i % 3]
        if i == self.bad_candle_at:
            row[2] = price - 10  # a high below the open: an impossible bar
        return row

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(("ohlcv", symbol, timeframe, since, limit))
        if symbol == "NOPE/USD":
            import ccxt

            raise ccxt.BadSymbol("unknown symbol")
        size = min(limit or self.page, self.page)
        if self.recent_only or since is None:
            first = max(0, self.history - size)
        else:
            first = max(0, -(-(since - T0) // MINUTE))
        return [self._candle(i) for i in range(first, min(first + size, self.history))]

    async def fetch_trades(self, symbol, since=None, limit=None):
        self.calls.append(("trades", symbol, since, limit))
        return [{"id": f"t{since + i * 1000}", "timestamp": since + i * 1000, "price": 100 + i * 0.1, "amount": 0.5 + (i % 2), "side": "buy" if i % 2 else "sell"}
                for i in range(20)]

    async def fetch_order_book(self, symbol, limit=None):
        self.calls.append(("book", symbol, limit))
        return {"bids": [[100.0, 1.0], [99.5, 2.0], [99.0, 4.0]][:limit], "asks": [[100.5, 3.0], [101.0, 3.0], [101.5, 1.0]][:limit], "timestamp": None, "nonce": 7}

    async def close(self):
        self.closed = True
