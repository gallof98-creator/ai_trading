"""CoinGecko: prices aggregated across exchanges, market capitalisation, and how a coin's volume and spread
are spread over venues. Complements the exchange feeds; it is not a tradable price.

Works without a key at a slow rate; a free Demo key raises the limit. The free plan serves one year of
daily and hourly history, has a monthly request allowance (enforced here as a budget) and asks for
attribution ("Data provided by CoinGecko") wherever the data is shown.
"""
from __future__ import annotations

import pandas as pd

from ..http import ApiClient, SourceError
from ..lake import now_utc

BASE_URL = "https://api.coingecko.com/api/v3"


class CoinGeckoSource:
    name = "coingecko"

    def __init__(self, api_key: str = "", base_url: str = BASE_URL) -> None:
        self._key, self.base_url = api_key, base_url

    def client(self, **kw) -> ApiClient:
        # Keyless access is shared and throttled hard; a Demo key is paced well inside its per-minute limit.
        return ApiClient("CoinGecko", self.base_url, interval=2.5 if self._key else 7.0, headers={"x-cg-demo-api-key": self._key} if self._key else {}, **kw)

    async def market_chart(self, client: ApiClient, coin_id: str, days: int = 365, vs_currency: str = "usd") -> pd.DataFrame:
        """Price, market capitalisation and 24h volume history (rows for `crypto_market`). The free plan serves at most 365
        days; granularity is chosen by CoinGecko (hourly up to 90 days, daily beyond)."""
        body = await client.get_json(f"coins/{coin_id}/market_chart", {"vs_currency": vs_currency, "days": min(int(days), 365)})
        if not isinstance(body, dict) or not isinstance(body.get("prices"), list):
            raise SourceError("CoinGecko returned an unexpected response")
        received = now_utc()
        frame = pd.DataFrame(body["prices"], columns=["ms", "price"])
        for name, column in (("market_caps", "market_cap"), ("total_volumes", "volume")):
            other = pd.DataFrame(body.get(name) or [], columns=["ms", column])
            frame = frame.merge(other, on="ms", how="left") if len(other) else frame.assign(**{column: float("nan")})
        frame["ts"] = pd.to_datetime(frame["ms"], unit="ms", utc=True).dt.as_unit("us")
        frame = frame.drop(columns="ms").drop_duplicates("ts", keep="last")
        # The last point is "now", a moving value: it would be rewritten on every request, so it is not stored as history.
        frame = frame[frame["ts"] < received.floor("h")].copy()
        frame["source"], frame["coin_id"], frame["vs_currency"], frame["received_at"] = self.name, coin_id, vs_currency, received
        return frame.reset_index(drop=True)

    async def venues(self, client: ApiClient, coin_id: str) -> pd.DataFrame:
        """Where the coin trades right now: per exchange and pair, last price, volume and quoted spread (rows for `crypto_venues`)."""
        body = await client.get_json(f"coins/{coin_id}/tickers", {"include_exchange_logo": "false", "depth": "false", "order": "volume_desc"})
        if not isinstance(body, dict) or not isinstance(body.get("tickers"), list):
            raise SourceError("CoinGecko returned an unexpected response")
        received = now_utc()
        rows = []
        for t in body["tickers"]:
            market = t.get("market") or {}
            if not market.get("identifier") or t.get("last") is None:
                continue
            rows.append({"venue": market["identifier"], "pair": f"{t.get('base')}/{t.get('target')}", "last": float(t["last"]),
                         "volume": float(t.get("volume") or 0), "volume_usd": float((t.get("converted_volume") or {}).get("usd") or 0),
                         "spread_pct": t.get("bid_ask_spread_percentage"), "trust_score": t.get("trust_score"),
                         "is_stale": bool(t.get("is_stale")), "is_anomaly": bool(t.get("is_anomaly")), "traded_at": t.get("last_traded_at")})
        frame = pd.DataFrame(rows, columns=["venue", "pair", "last", "volume", "volume_usd", "spread_pct", "trust_score", "is_stale", "is_anomaly", "traded_at"])
        frame["spread_pct"] = pd.to_numeric(frame["spread_pct"], errors="coerce")
        frame["traded_at"] = pd.to_datetime(frame["traded_at"], utc=True, errors="coerce").dt.as_unit("us")
        frame = frame.drop_duplicates(["venue", "pair"], keep="first")
        frame["ts"] = received.floor("min")  # a snapshot: its event time is when it was taken
        frame["source"], frame["coin_id"], frame["received_at"] = self.name, coin_id, received
        return frame.reset_index(drop=True)
