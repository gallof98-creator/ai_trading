"""FRED / ALFRED (Federal Reserve Bank of St. Louis): macroeconomic series with their publication history.

Point-in-time rules, so that a backtest never sees a number before the market did:

* "vintage" series (CPI, PCE, payrolls, GDP ...) are revised after the first release. Every value is stored
  together with the real-time period in which it was the published figure (ALFRED). `available_at` is the
  evening (18:00 New York) of the day that vintage was published. The release usually happens in the
  morning; the scheduled time can be supplied when the data is read, the stored value stays conservative.
* "lag" series (Treasury yields, effective fed funds, dollar index) are daily market series that are not
  revised in any way that matters and have far too many vintages to download. Their `available_at` is
  18:00 New York on the next business day, which is after the H.15 release.

Free API key required (fred.stlouisfed.org). Limit: 120 requests a minute.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..http import ApiClient, SourceAuthError, SourceError, SourceRateLimited
from ..lake import now_utc

BASE_URL = "https://api.stlouisfed.org/fred"
NY = "America/New_York"


@dataclass(frozen=True)
class MacroSeries:
    id: str
    title: str
    cadence: str  # D | W | M | Q
    pit: str  # vintage | lag
    release_time: str = ""  # usual release time in New York, for information (not applied to available_at)


SERIES: dict[str, MacroSeries] = {s.id: s for s in (
    MacroSeries("DFF", "Effective federal funds rate", "D", "lag"),
    MacroSeries("DFEDTARU", "Federal funds target range, upper limit", "D", "lag"),
    MacroSeries("DFEDTARL", "Federal funds target range, lower limit", "D", "lag"),
    MacroSeries("DGS3MO", "Treasury yield, 3 months", "D", "lag"),
    MacroSeries("DGS2", "Treasury yield, 2 years", "D", "lag"),
    MacroSeries("DGS10", "Treasury yield, 10 years", "D", "lag"),
    MacroSeries("DGS30", "Treasury yield, 30 years", "D", "lag"),
    MacroSeries("DTWEXBGS", "Nominal broad US dollar index", "D", "lag"),
    MacroSeries("DEXUSEU", "US dollars per euro", "D", "lag"),
    MacroSeries("CPIAUCSL", "Consumer price index, all items (SA)", "M", "vintage", "08:30"),
    MacroSeries("CPILFESL", "Consumer price index, core (SA)", "M", "vintage", "08:30"),
    MacroSeries("PCEPI", "PCE price index", "M", "vintage", "08:30"),
    MacroSeries("PCEPILFE", "PCE price index, core", "M", "vintage", "08:30"),
    MacroSeries("UNRATE", "Unemployment rate", "M", "vintage", "08:30"),
    MacroSeries("PAYEMS", "Nonfarm payrolls", "M", "vintage", "08:30"),
    MacroSeries("ICSA", "Initial jobless claims", "W", "vintage", "08:30"),
    MacroSeries("GDPC1", "Real gross domestic product", "Q", "vintage", "08:30"),
    MacroSeries("INDPRO", "Industrial production index", "M", "vintage", "09:15"),
)}
CADENCE_SECONDS = {"D": 86400, "W": 7 * 86400, "M": 31 * 86400, "Q": 92 * 86400}


def _evening(dates: pd.Series) -> pd.Series:
    """18:00 New York on the given calendar dates, as UTC."""
    return (pd.to_datetime(dates) + pd.Timedelta(hours=18)).dt.tz_localize(NY).dt.tz_convert("UTC").dt.as_unit("us")


class FredSource:
    name = "fred"

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        if not api_key:
            raise SourceAuthError("No FRED API key is configured")
        self._key, self.base_url = api_key, base_url

    def client(self, **kw) -> ApiClient:
        return ApiClient("FRED", self.base_url, interval=0.55, params={"api_key": self._key, "file_type": "json"}, **kw)

    async def _pages(self, client: ApiClient, params: dict) -> list[dict]:
        rows, offset = [], 0
        while True:
            body = await client.get_json("series/observations", {**params, "limit": 100000, "offset": offset})
            if not isinstance(body, dict) or not isinstance(body.get("observations"), list):
                raise SourceError("FRED returned an unexpected response")
            rows.extend(body["observations"])
            offset += len(body["observations"])
            if not body["observations"] or offset >= int(body.get("count", 0)):
                return rows

    async def observations(self, client: ApiClient, series_id: str, start: date | None = None, pit: str | None = None) -> tuple[pd.DataFrame, dict]:
        """(rows for the `macro` dataset, notes). `pit` overrides the series' default point-in-time rule."""
        pit = pit or (SERIES[series_id].pit if series_id in SERIES else "lag")
        params = {"series_id": series_id}
        if start is not None:
            params["observation_start"] = start.isoformat()
        notes: dict = {"pit": pit}
        raw: list[dict] | None = None
        if pit == "vintage":
            try:
                raw = await self._pages(client, {**params, "realtime_start": "1776-07-04", "realtime_end": "9999-12-31"})
            except (SourceAuthError, SourceRateLimited):
                raise
            except SourceError as exc:  # e.g. more vintages than one request may return
                notes.update(pit="lag", fallback=f"vintages unavailable ({exc}); the next-business-day rule is used instead")
                pit = "lag"
        if raw is None:
            raw = await self._pages(client, params)
        received = now_utc()
        frame = pd.DataFrame(raw, columns=["realtime_start", "realtime_end", "date", "value"])
        notes["rows_received"] = len(frame)
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")  # FRED writes "." for a missing value
        notes["missing_values"] = int(frame["value"].isna().sum())
        frame = frame[frame["value"].notna()].copy()
        day = pd.to_datetime(frame["date"])
        frame["date"] = day.dt.tz_localize("UTC").dt.as_unit("us")
        if pit == "vintage":
            first = pd.to_datetime(frame["realtime_start"])
            frame["available_at"] = _evening(first)
            last = frame["realtime_end"].where(frame["realtime_end"] != "9999-12-31")
            frame["realtime_end"] = pd.to_datetime(last).dt.tz_localize("UTC").dt.as_unit("us")
            frame["realtime_start"] = first.dt.tz_localize("UTC").dt.as_unit("us")
        else:
            frame["available_at"] = _evening(day + pd.offsets.BDay(1))
            frame["realtime_start"] = frame["date"]  # one row per observation: the key needs a value, the rule is in `availability`
            frame["realtime_end"] = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[us, UTC]")
        frame["availability"] = pit if pit == "vintage" else "lag_rule"
        frame["series_id"], frame["source"], frame["received_at"] = series_id, self.name, received
        return frame[["source", "series_id", "date", "value", "realtime_start", "realtime_end", "available_at", "availability", "received_at"]], notes
