"""Chart datafeed: symbol search, symbol resolution, OHLCV history and chart studies.

The endpoints follow the shape of TradingView's Datafeed API (search / resolve / history with
`s,t,o,h,l,c,v` arrays), so the charting component can be replaced without touching the backend.
The chart is a view: every number comes from the bars stored by the platform's own providers,
and the provenance (historical, delayed, real-time, simulated) is always reported.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database.models import BarSeries, Security, SignalRow
from ..global_markets import catalog
from ..global_markets.exchanges import EXCHANGES
from ..instruments.registry import market_of
from ..market_data.calendars import get_calendar
from ..market_data.store import BarStore
from ..market_data.timeframes import OHLCV, TIMEFRAMES, tf_delta
from ..quant_ai.regime import detect_regimes
from ..strategies import indicators as ind

# TradingView resolution -> platform timeframe. "1M" has no stored timeframe: it is built from daily bars.
RESOLUTIONS = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "240": "4h", "1D": "1d", "1W": "1w", "1M": "1M"}
_RULE = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON", "1M": "MS"}
_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
MAX_BARS = 5000
_store = BarStore()


class DatafeedError(ValueError):
    pass


def _span(tf: str) -> pd.Timedelta:
    return pd.Timedelta(days=31) if tf == "1M" else tf_delta(tf)


def _base_for(stored: list[str], target: str) -> str | None:
    """The coarsest stored timeframe that can build `target` exactly (never a finer chart from coarser bars)."""
    usable = [tf for tf in stored if tf in TIMEFRAMES and _ORDER.index(tf) <= _ORDER.index(target)
              and (target == "1M" or tf_delta(target) % tf_delta(tf) == pd.Timedelta(0)) and not (target == "1M" and tf == "1w")]
    return max(usable, key=_ORDER.index) if usable else None


def _coverage(session: Session, instrument_id: str) -> dict[str, BarSeries]:
    return {r.timeframe: r for r in session.scalars(select(BarSeries).where(BarSeries.instrument_id == instrument_id))}


def config() -> dict:
    return {"supported_resolutions": list(RESOLUTIONS), "supports_search": True, "supports_marks": True, "supports_time": True,
            "streaming": {"mode": "polling", "note": "No push feed: the last bar is refreshed by polling the stored data."},
            "note": "A resolution is offered for a symbol only when stored bars can build it exactly."}


# ---------------------------------------------------------------- symbols
def _from_security(s: Security) -> dict:
    ex = EXCHANGES.get(s.exchange)
    return {"symbol": s.instrument_id or f"sec:{s.id}", "ticker": s.ticker, "description": s.name, "exchange": s.exchange, "mic": s.mic,
            "country": s.country, "currency": s.currency, "type": s.security_type, "isin": s.isin, "figi": s.figi, "sector": s.sector, "industry": s.industry,
            "listing_status": s.listing_status, "security_id": s.id, "instrument_id": s.instrument_id, "timezone": ex.timezone if ex else "Etc/UTC",
            "provider_symbols": {k: v.get("symbol") for k, v in (s.sources or {}).items()}}


def _from_instrument(inst, security: Security | None = None) -> dict:
    if security is not None:
        return _from_security(security)
    market = market_of(inst)
    return {"symbol": inst.id, "ticker": inst.symbol, "description": "", "exchange": inst.exchange or market or inst.asset_class.value, "mic": None, "country": None,
            "currency": inst.quote_currency, "type": inst.asset_class.value, "isin": None, "figi": None, "sector": None, "industry": None,
            "listing_status": "active", "security_id": None, "instrument_id": inst.id, "timezone": get_calendar(inst.calendar).tz,
            "provider_symbols": {}}


def search(session: Session, registry, q: str = "", exchange: list[str] | None = None, types: list[str] | None = None, sector: str | None = None,
           has_data: bool | None = None, limit: int = 30) -> list[dict]:
    """Everything the platform knows: the security catalog plus instruments that are not in it (crypto, forex, futures)."""
    limit = max(1, min(limit, 200))
    found = catalog.search(session, q, exchange or None, None, types or None, "active", has_data, limit if not sector else 500)["items"]
    if sector:
        found = [f for f in found if (f.get("sector") or "").lower() == sector.lower()][:limit]
    priced = {r.instrument_id for r in session.scalars(select(BarSeries))}
    out, seen = [], set()
    for f in found:
        sid = f["instrument_id"] or f"sec:{f['id']}"
        seen.add(f["instrument_id"])
        out.append({"symbol": sid, "ticker": f["ticker"], "description": f["name"], "exchange": f["exchange"], "type": f["security_type"], "currency": f["currency"],
                    "isin": f["isin"], "figi": f["figi"], "sector": f["sector"], "has_data": f["instrument_id"] in priced, "in_universe": f["instrument_id"] is not None})
    text = q.strip().upper()
    if not sector and not exchange and len(out) < limit:
        for inst in registry.all():
            if inst.id in seen or (types and inst.asset_class.value not in types):
                continue
            if text and text not in inst.symbol.upper() and text not in inst.id.upper():
                continue
            if has_data is not None and (inst.id in priced) != has_data:
                continue
            out.append({"symbol": inst.id, "ticker": inst.symbol, "description": "", "exchange": inst.exchange or market_of(inst) or inst.asset_class.value,
                        "type": inst.asset_class.value, "currency": inst.quote_currency, "isin": None, "figi": None, "sector": None,
                        "has_data": inst.id in priced, "in_universe": True})
            if len(out) >= limit:
                break
    # Symbols with stored prices first: those are the ones a chart can actually show.
    return sorted(out, key=lambda r: (not r["has_data"], not r["in_universe"]))[:limit]


def resolve(session: Session, registry, symbol: str) -> dict:
    """Accepts a platform instrument id, `sec:<id>`, an ISIN, a FIGI, `EXCHANGE:TICKER` or a bare ticker."""
    text = symbol.strip()
    if not text:
        raise DatafeedError("Empty symbol")
    inst = security = None
    alternatives = 0
    try:
        inst = registry.get(text)
    except KeyError:
        pass
    if inst is not None:
        security = session.scalar(select(Security).where(Security.instrument_id == inst.id))
    elif text.lower().startswith("sec:") and text[4:].isdigit():
        security = session.get(Security, int(text[4:]))
    else:
        upper = text.upper()
        if re.fullmatch(r"BBG[A-Z0-9]{9}", upper):  # checked before ISIN: a FIGI also fits the ISIN pattern
            rows = list(session.scalars(select(Security).where(or_(Security.figi == upper, Security.composite_figi == upper, Security.share_class_figi == upper))))
        elif re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", upper):
            rows = list(session.scalars(select(Security).where(Security.isin == upper)))
        elif ":" in upper and upper.split(":", 1)[0] in EXCHANGES:
            code, ticker = upper.split(":", 1)
            rows = list(session.scalars(select(Security).where(Security.exchange == code, Security.ticker == ticker)))
        else:
            rows = list(session.scalars(select(Security).where(Security.ticker == upper)))
        # Several listings can share an identifier: prefer active ones that are in the trading universe, and say how many there were.
        rows.sort(key=lambda s: (s.listing_status != "active", s.instrument_id is None, s.exchange))
        alternatives = max(0, len(rows) - 1)
        security = rows[0] if rows else None
    if security is None and inst is None:
        raise DatafeedError(f"Unknown symbol {symbol!r}")
    if inst is None and security.instrument_id:
        try:
            inst = registry.get(security.instrument_id)
        except KeyError:
            inst = None
    info = _from_instrument(inst, security) if inst is not None else _from_security(security)
    info["other_matches"] = alternatives
    cov = _coverage(session, inst.id) if inst is not None else {}
    stored = sorted(cov, key=lambda t: _ORDER.index(t) if t in _ORDER else 99)
    info["stored_timeframes"] = stored
    info["supported_resolutions"] = [r for r, tf in RESOLUTIONS.items() if _base_for(stored, tf)]
    info["has_data"] = bool(stored)
    info["pricescale"] = int(round(1 / inst.tick_size)) if inst is not None else 100
    if stored:
        newest = max(cov.values(), key=lambda r: r.last_ms)
        last = pd.Timestamp(newest.last_ms, unit="ms", tz="UTC")
        kinds = {r.kind for r in cov.values()}
        kind = "simulated" if "simulated" in kinds else "realtime" if kinds == {"realtime"} else "delayed" if "delayed" in kinds else "historical"
        age = (pd.Timestamp(datetime.now(timezone.utc)) - last) / pd.Timedelta(days=1)
        info["data"] = {"kind": kind, "sources": sorted({r.source for r in cov.values()}), "last_bar": last.isoformat(), "age_days": round(float(age), 1),
                        "bars": {tf: r.bars for tf, r in cov.items()},
                        "note": {"simulated": "Simulated prices generated by the platform, not market data.",
                                 "historical": "End-of-day history from a data provider. Not real-time: the last bar is the last one downloaded.",
                                 "delayed": "Delayed market data.", "realtime": "Bars received from a connected broker feed."}[kind]}
    else:
        info["data"] = {"kind": "none", "sources": [], "last_bar": None, "age_days": None, "bars": {},
                        "note": "No prices stored for this symbol." + ("" if inst is not None else " It is in the catalog but not in the trading universe: add it from Global Markets, then download prices.")}
    return info


# ---------------------------------------------------------------- bars
def load_bars(session: Session, instrument_id: str, resolution: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
              warmup: int = 0) -> tuple[pd.DataFrame, str]:
    """Bars at the requested resolution, built from the coarsest stored timeframe that divides it. Returns (bars, data kind).
    The still-forming last bar is kept (a chart shows it); strategies never read bars from here."""
    if resolution not in RESOLUTIONS:
        raise DatafeedError(f"Unsupported resolution {resolution!r}; supported: {', '.join(RESOLUTIONS)}")
    target = RESOLUTIONS[resolution]
    base = _base_for(list(_coverage(session, instrument_id)), target)
    if base is None:
        raise DatafeedError(f"No stored bars can build the {resolution} resolution for {instrument_id}")
    if start is not None:
        start = start - _span(target) * (warmup + 1)
    ds = _store.load(session, instrument_id, base, start=start, end=end)
    if ds is None:
        return pd.DataFrame(columns=OHLCV), "none"
    bars = ds.bars
    if base != target:
        bars = (bars[OHLCV].resample(_RULE[target], label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"]))
    return bars, ds.kind.value


def history(session: Session, instrument_id: str, resolution: str, from_ts: int | None, to_ts: int | None, countback: int | None = None) -> dict:
    """Datafeed `getBars`: bars with open time in [from, to], or the last `countback` bars ending at `to`. Times are epoch seconds."""
    end = pd.Timestamp(to_ts, unit="s", tz="UTC") if to_ts else None
    start = pd.Timestamp(from_ts, unit="s", tz="UTC") if from_ts and not countback else None
    bars, kind = load_bars(session, instrument_id, resolution, start, end)
    if end is not None and len(bars):
        bars = bars[bars.index <= end]
    if countback:
        bars = bars.iloc[-min(countback, MAX_BARS):]
    elif start is not None and len(bars):
        bars = bars[bars.index >= start]
    bars = bars.iloc[-MAX_BARS:]
    if bars.empty:
        earlier, _ = load_bars(session, instrument_id, resolution, None, pd.Timestamp(from_ts, unit="s", tz="UTC") if from_ts else end)
        return {"s": "no_data", "nextTime": int(earlier.index[-1].timestamp()) if len(earlier) else None}
    return {"s": "ok", "t": [int(t.timestamp()) for t in bars.index], "o": bars["open"].tolist(), "h": bars["high"].tolist(), "l": bars["low"].tolist(),
            "c": bars["close"].tolist(), "v": bars["volume"].tolist(), "kind": kind}


# ---------------------------------------------------------------- studies
def support_resistance(bars: pd.DataFrame, lookback: int = 250, k: int = 5, tolerance: float = 0.015, per_side: int = 3) -> list[dict]:
    """Price levels where confirmed swing highs/lows cluster. A pivot counts only once `k` later bars confirm it,
    so a level never depends on bars that had not happened yet. Strength = number of pivots in the cluster."""
    recent = bars.iloc[-lookback:]
    if len(recent) < 4 * k:
        return []
    highs, lows = ind.confirmed_pivots(recent, k)
    prices = sorted(pd.concat([highs.dropna(), lows.dropna()]).unique())
    if not prices:
        return []
    clusters: list[list[float]] = [[prices[0]]]
    for p in prices[1:]:
        if p <= np.mean(clusters[-1]) * (1 + tolerance):
            clusters[-1].append(p)
        else:
            clusters.append([p])
    last = float(recent["close"].iloc[-1])
    levels = [{"price": float(np.mean(c)), "touches": len(c), "kind": "resistance" if np.mean(c) > last else "support",
               "distance": float(np.mean(c) / last - 1)} for c in clusters]
    below = sorted((x for x in levels if x["kind"] == "support"), key=lambda x: -x["price"])[:per_side]
    above = sorted((x for x in levels if x["kind"] == "resistance"), key=lambda x: x["price"])[:per_side]
    return below + above


def _col(series: pd.Series, digits: int = 6) -> list[float | None]:
    return [None if (v is None or not math.isfinite(v)) else round(float(v), digits) for v in series.to_numpy(dtype=float)]


def studies(session: Session, instrument_id: str, resolution: str, bars_wanted: int = 600) -> dict:
    """Indicators aligned with the last `bars_wanted` bars, trend and regime, support/resistance levels and recorded signals."""
    full, kind = load_bars(session, instrument_id, resolution)
    if full.empty:
        raise DatafeedError("No bars stored for this symbol and resolution")
    full = full.iloc[-(bars_wanted + 300):]  # warm-up for the 200-bar average
    n = min(bars_wanted, len(full))
    c = full["close"]
    mid, upper, lower = ind.bollinger(c, 20, 2.0)
    line, signal = ind.macd(c)
    tail = lambda s: _col(s.iloc[-n:])  # noqa: E731
    out = {"kind": kind, "t": [int(t.timestamp()) for t in full.index[-n:]],
           "overlays": {"sma_20": tail(ind.sma(c, 20)), "sma_50": tail(ind.sma(c, 50)), "sma_200": tail(ind.sma(c, 200)), "ema_20": tail(ind.ema(c, 20)),
                        "bb_upper": tail(upper), "bb_lower": tail(lower)},
           "panes": {"rsi_14": tail(ind.rsi(c, 14)), "macd": tail(line), "macd_signal": tail(signal), "macd_hist": tail(line - signal),
                     "atr_14": tail(ind.atr(full, 14))},
           "levels": support_resistance(full), "trend": None, "regime": None, "signals": []}
    if len(full) >= 60:
        regimes = detect_regimes(full)["primary"]
        out["regime"] = None if regimes.iloc[-1] == "unknown" else str(regimes.iloc[-1])
    s50, s200 = ind.sma(c, 50).iloc[-1], ind.sma(c, 200).iloc[-1]
    if math.isfinite(s50) and math.isfinite(s200):
        last = float(c.iloc[-1])
        out["trend"] = "up" if last > s50 > s200 else "down" if last < s50 < s200 else "mixed"
    first = full.index[-n].to_pydatetime()
    rows = session.scalars(select(SignalRow).where(SignalRow.instrument_id == instrument_id, SignalRow.ts >= first, SignalRow.action != "HOLD")
                           .order_by(SignalRow.ts.desc()).limit(300))
    out["signals"] = [{"time": int(pd.Timestamp(r.ts).tz_localize("UTC").timestamp() if pd.Timestamp(r.ts).tzinfo is None else pd.Timestamp(r.ts).timestamp()),
                       "action": r.action, "strategy": r.strategy, "confidence": (r.payload or {}).get("confidence")} for r in rows][::-1]
    return out
