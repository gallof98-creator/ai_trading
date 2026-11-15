"""Alpaca historical intraday bars for US stocks, ETFs and crypto, with the account the platform already uses.

This extends the existing Alpaca integration (daily bars, live stream) to minute history; it adds no new
vendor. Stock bars come from the consolidated tape (SIP) and include the trade count and the VWAP of each
bar. The free plan may not query the most recent 15 minutes of the tape, so requests stop 16 minutes
before now and the data is DELAYED, never real time. Prices are adjusted for splits and dividends, like
the platform's daily Alpaca history. About 200 requests a minute; 10,000 bars per request.

VERIFICATION STATUS: written from the vendor's documentation, same endpoint and parameters as the
platform's daily download; not yet exercised with real keys at minute resolution.
"""
from __future__ import annotations

import pandas as pd

from ...market_data.timeframes import OHLCV, tf_delta
from ..http import ApiClient, SourceAuthError, SourceError
from ..lake import now_utc

BASE_URL = "https://data.alpaca.markets"
_TF = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day"}
SIP_DELAY = pd.Timedelta(minutes=16)


class AlpacaBars:
    name = "alpaca"

    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL) -> None:
        if not api_key or not api_secret:
            raise SourceAuthError("Alpaca needs both the API key id and the secret key")
        self._key, self._secret, self.base_url = api_key, api_secret, base_url

    def client(self, **kw) -> ApiClient:
        return ApiClient("Alpaca", self.base_url, interval=0.35, headers={"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}, **kw)

    async def bars(self, client: ApiClient, symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp | None = None,
                   asset: str = "stocks", max_pages: int = 2000) -> tuple[pd.DataFrame, dict]:
        """Completed bars with open time in [start, end] (rows for the `bars` dataset). `asset` is "stocks" or "crypto"."""
        if timeframe not in _TF:
            raise SourceError(f"Alpaca does not serve the {timeframe} timeframe here")
        now = now_utc()
        end = min(end or now, now - SIP_DELAY) if asset == "stocks" else (end or now)
        if end <= start:
            return pd.DataFrame(), {"pages": 0}
        path = "v2/stocks/bars" if asset == "stocks" else "v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": _TF[timeframe], "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "sort": "asc"}
        if asset == "stocks":
            params.update(adjustment="all", feed="sip")
        rows, notes = [], {"pages": 0}
        for _ in range(max_pages):  # bounded so a faulty page token cannot loop forever
            body = await client.get_json(path, params)
            notes["pages"] += 1
            if not isinstance(body, dict) or not isinstance(body.get("bars"), dict):
                raise SourceError("Alpaca returned an unexpected response")
            rows.extend(body["bars"].get(symbol) or [])
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        received = now_utc()
        frame = pd.DataFrame([(b.get("t"), b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v"), b.get("n"), b.get("vw")) for b in rows],
                             columns=["ts", *OHLCV, "trades", "vwap"])
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce").dt.as_unit("us")
        frame = frame[frame["ts"].notna()].copy()
        for name in (*OHLCV, "trades", "vwap"):
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        forming = frame["ts"] + tf_delta(timeframe) > received
        notes["forming_dropped"] = int(forming.sum())
        frame = frame[~forming].copy()
        frame["source"], frame["symbol"], frame["timeframe"], frame["received_at"] = self.name, symbol, timeframe, received
        frame["venue"] = "SIP (15 min delayed, adjusted)" if asset == "stocks" else "Alpaca crypto"
        return frame.reset_index(drop=True), notes
