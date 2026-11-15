"""Read models over the lake for the API and the AI research layer: JSON-safe, bounded in size, point in time."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import features
from .lake import now_utc
from .service import DataHub, DataHubError, _ts
from .sources.fred import SERIES

MAX_ROWS = 5000
ATTRIBUTION = {
    "fred": "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.",
    "coingecko": "Data provided by CoinGecko.",
    "sec": "Source: U.S. Securities and Exchange Commission, EDGAR.",
}


def records(frame: pd.DataFrame, limit: int = MAX_ROWS) -> list[dict]:
    """Rows as JSON-safe dictionaries (ISO timestamps, null for missing), keeping the last `limit`."""
    frame = frame.iloc[-max(1, min(limit, MAX_ROWS)):]
    out = []
    for row in frame.to_dict("records"):
        clean = {}
        for key, value in row.items():
            if isinstance(value, pd.Timestamp):
                clean[key] = None if pd.isna(value) else value.isoformat()
            elif isinstance(value, np.ndarray):
                clean[key] = value.tolist()
            elif isinstance(value, (float, np.floating)):
                clean[key] = None if not math.isfinite(value) else float(value)
            elif isinstance(value, np.integer):
                clean[key] = int(value)
            elif isinstance(value, np.bool_):
                clean[key] = bool(value)
            elif value is pd.NaT or value is pd.NA:
                clean[key] = None
            else:
                clean[key] = value
        out.append(clean)
    return out


def bars(hub: DataHub, source: str, symbol: str, timeframe: str, start=None, end=None, limit: int = 1000) -> dict:
    frame = hub.lake.read("bars", _ts(start), _ts(end), source=source, symbol=symbol, timeframe=timeframe, limit=max(1, min(limit, MAX_ROWS)), newest_first=True)
    if frame.empty:
        raise LookupError(f"No stored bars for {source} {symbol} {timeframe}. Ingest them first.")
    return {"source": source, "symbol": symbol, "timeframe": timeframe, "rows": records(frame.iloc[::-1].reset_index(drop=True), limit)}


def _split(label: str) -> tuple[str, str]:
    if ":" not in label:
        raise DataHubError(f"Expected source:symbol, got {label!r}")
    source, symbol = label.split(":", 1)
    return source.strip().lower(), symbol.strip()


def closes(hub: DataHub, labels: list[str], timeframe: str, start=None, end=None) -> dict[str, pd.Series]:
    out = {}
    for label in labels:
        source, symbol = _split(label)
        frame = hub.lake.bars(source, symbol, timeframe, _ts(start), _ts(end))
        if frame.empty:
            raise LookupError(f"No stored bars for {source} {symbol} {timeframe}. Ingest them first.")
        out[label] = frame["close"]
    return out


def compare(hub: DataHub, labels: list[str], timeframe: str, start=None, end=None, limit: int = 500, window: int = 240) -> dict:
    """Divergence between venues, on the timestamps they all have. Nothing is merged or filled."""
    if len(labels) < 2:
        raise DataHubError("Give at least two source:symbol series to compare")
    series = closes(hub, labels, timeframe, start, end)
    frame = features.compute("cross_venue", series, window=window)
    panel = pd.concat(series, axis=1, join="inner")
    return {"timeframe": timeframe, "feature_set": frame.attrs["feature_set"], "series": labels, "common_bars": len(panel),
            "bars_per_series": {k: int(len(v)) for k, v in series.items()},
            "rows": records(pd.concat([panel.add_prefix("close_"), frame], axis=1).reset_index(names="ts"), limit)}


def macro_state(hub: DataHub, asof=None, series: list[str] | None = None, release_times: dict[str, str] | None = None) -> dict:
    """What was known about each macro series at `asof` (default: now)."""
    moment = _ts(asof) or now_utc()
    rows = hub.lake.read("macro", source="fred")
    if rows.empty:
        raise LookupError("No macro data stored. Ingest FRED first.")
    if series:
        rows = rows[rows["series_id"].isin([s.upper() for s in series])]
    frame = features.compute("macro", rows, pd.DatetimeIndex([moment]), release_times=release_times)
    values = records(frame)[0]
    out = []
    for series_id, group in rows.groupby("series_id"):
        known = features.latest_known(features._with_release_times(group, release_times))
        known = known[known["available_at"] <= moment]
        if known.empty:
            continue
        last = known.iloc[-1]
        info = SERIES.get(series_id)
        out.append({"series_id": series_id, "title": info.title if info else "", "value": float(last["value"]), "observation_date": last["date"].date().isoformat(),
                    "published_at": last["available_at"].isoformat(), "age_days": values.get(f"{series_id}_age_days"),
                    "point_in_time": group["availability"].iloc[0]})
    derived = {k: v for k, v in values.items() if k.startswith("yc_") or k.endswith("_yoy") or "_chg_" in k}
    return {"asof": moment.isoformat(), "feature_set": frame.attrs["feature_set"], "series": out, "derived": derived, "attribution": ATTRIBUTION["fred"]}


def macro_series(hub: DataHub, series_id: str, vintages: bool = False, limit: int = 2000) -> dict:
    rows = hub.lake.read("macro", source="fred", series_id=series_id.upper())
    if rows.empty:
        raise LookupError(f"No stored observations for {series_id}. Ingest FRED first.")
    if not vintages:  # the figures as they stand today: the latest vintage of each observation
        rows = rows.sort_values(["date", "available_at"]).drop_duplicates("date", keep="last")
    columns = ["date", "value", "realtime_start", "realtime_end", "available_at", "availability"]
    return {"series_id": series_id.upper(), "vintages": vintages, "rows": records(rows[columns].reset_index(drop=True), limit), "attribution": ATTRIBUTION["fred"]}


def _cik(hub: DataHub, ticker: str, dataset: str) -> str:
    for row in hub.catalog(dataset, "sec"):
        if row["key"].upper() == ticker.upper() or (row["detail"] or {}).get("ticker", "").upper() == ticker.upper():
            return (row["detail"] or {}).get("cik") or row["key"]
    raise LookupError(f"No {dataset} stored for {ticker}. Ingest SEC EDGAR for it first.")


def fundamentals(hub: DataHub, ticker: str, asof=None, concepts: list[str] | None = None, limit: int = 2000) -> dict:
    moment = _ts(asof) or now_utc()
    facts = hub.lake.read("fundamentals", source="sec", cik=_cik(hub, ticker, "fundamentals"))
    known = features.fundamentals_asof(facts, moment, concepts)
    columns = ["taxonomy", "concept", "unit", "period_start", "period_end", "duration_days", "value", "fiscal_year", "fiscal_period", "form", "filed",
               "accession", "available_at", "availability"]
    return {"ticker": ticker.upper(), "asof": moment.isoformat(), "facts": len(known), "rows": records(known[columns], limit), "attribution": ATTRIBUTION["sec"]}


def filings(hub: DataHub, ticker: str, forms: list[str] | None = None, limit: int = 200) -> dict:
    rows = hub.lake.read("filings", source="sec", cik=_cik(hub, ticker, "filings"))
    if forms:
        rows = rows[rows["form"].isin(forms)]
    columns = ["accession", "form", "filing_date", "report_date", "ts", "primary_document", "items", "is_xbrl"]
    return {"ticker": ticker.upper(), "rows": records(rows[columns].rename(columns={"ts": "accepted_at"}), limit), "attribution": ATTRIBUTION["sec"]}


def feature_rows(hub: DataHub, name: str, source: str, symbol: str, timeframe: str = "1m", start=None, end=None, limit: int = 500, **params) -> dict:
    """Compute one single-series feature set on stored data."""
    start, end = _ts(start), _ts(end)
    if name == "bars_intraday":
        data = hub.lake.bars(source, symbol, timeframe, start, end)
        frame = features.compute(name, data, **params) if len(data) else data
    elif name == "orderbook":
        data = hub.lake.read("orderbook", start, end, source=source, symbol=symbol)
        frame = features.compute(name, data) if len(data) else data
    elif name == "trade_flow":
        data = hub.lake.read("trades", start, end, source=source, symbol=symbol)
        frame = features.compute(name, data, timeframe) if len(data) else data
    else:
        raise DataHubError(f"{name!r} is not a single-series feature set; available here: bars_intraday, orderbook, trade_flow")
    if frame.empty:
        raise LookupError(f"No stored data for {source} {symbol}. Ingest it first.")
    return {"feature_set": frame.attrs["feature_set"], "source": source, "symbol": symbol, "timeframe": timeframe,
            "rows": records(frame.reset_index(names="ts"), limit)}


def context(hub: DataHub, asof=None) -> dict:
    """A compact, structured picture of what the platform knows, for the AI research layer: macro state, cross-venue
    crypto prices, coverage and data-quality caveats. Every number carries where it came from and how old it is."""
    moment = _ts(asof) or now_utc()
    catalog = hub.catalog()
    out: dict = {"asof": moment.isoformat(), "macro": None, "crypto": [], "coverage": {}, "stale": [], "caveats": [
        "Macro figures are the ones published by the stated time; later revisions are not included.",
        "Prices of different venues are different instruments and are shown side by side, never blended.",
        "US stock bars from Alpaca's free plan are delayed by 15 minutes."]}
    try:
        out["macro"] = macro_state(hub, moment)
    except LookupError:
        pass
    by_base: dict[str, list[dict]] = {}
    for row in catalog:
        out["coverage"].setdefault(row["dataset"], {"series": 0, "rows": 0})
        out["coverage"][row["dataset"]]["series"] += 1
        out["coverage"][row["dataset"]]["rows"] += row["rows"]
        if row["stale"]:
            out["stale"].append({k: row[k] for k in ("dataset", "provider", "key", "timeframe", "last", "age_seconds")})
        if row["dataset"] == "bars" and "/" in row["key"]:
            by_base.setdefault(f"{row['key'].split('/')[0]}|{row['timeframe']}", []).append(row)
    for group, rows in sorted(by_base.items()):
        base, timeframe = group.split("|")
        venues = []
        for row in rows:
            last = hub.lake.read("bars", end=moment, source=row["provider"], symbol=row["key"], timeframe=timeframe, limit=1, newest_first=True,
                                 columns=["ts", "close", "volume"])
            if len(last):
                venues.append({"source": row["provider"], "symbol": row["key"], "bar_open": last["ts"].iloc[0].isoformat(), "close": float(last["close"].iloc[0]),
                               "age_seconds": round((moment - last["ts"].iloc[0]).total_seconds(), 1)})
        if venues:
            out["crypto"].append({"asset": base, "timeframe": timeframe, "venues": venues})
    out["attribution"] = [text for name, text in ATTRIBUTION.items() if any(r["provider"] == name for r in catalog)]
    return out
