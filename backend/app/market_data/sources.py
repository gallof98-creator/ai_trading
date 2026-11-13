"""Historical data sources. Every source declares the provenance (`kind`) of what it returns."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .quality import DataKind
from .timeframes import OHLCV, epoch_ns, tf_delta


@dataclass
class Dataset:
    bars: pd.DataFrame
    kind: DataKind
    source: str

    def fingerprint(self) -> str:
        """Content hash, stored with every backtest so a result can be tied to its exact inputs."""
        h = hashlib.sha256()
        h.update(epoch_ns(self.bars.index).tobytes())
        h.update(np.ascontiguousarray(self.bars[OHLCV].to_numpy(dtype="float64")).tobytes())
        return h.hexdigest()


_ALIASES = {"date": "timestamp", "datetime": "timestamp", "time": "clock", "vol": "volume", "per": "period"}


def load_csv(path, tz: str | None = None) -> Dataset:
    """Read OHLCV bars from CSV. Accepted layouts:

    * timestamp,open,high,low,close,volume            (this platform's own format)
    * Date,Open,High,Low,Close[,Adj Close],Volume      (common vendor export; extra columns ignored)
    * <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,...,<VOL>    (bulk-download text files; dates as YYYYMMDD)

    Date-only values are daily bars and are stamped at 00:00 UTC of that trading date. Values that
    carry a time but no UTC offset need `tz`, the timezone they are expressed in.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().strip("<>").strip().lower() for c in df.columns]
    df = df.rename(columns=_ALIASES)
    if "timestamp" not in df.columns:
        raise ValueError("CSV needs a 'timestamp' or 'date' column")
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    raw = df["timestamp"].astype(str).str.strip()
    compact = raw.str.fullmatch(r"\d{8}").all()
    date_only = compact or raw.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()
    clock = df["clock"].astype(str).str.strip().str.zfill(6) if "clock" in df.columns else None
    if date_only and (clock is None or (clock == "000000").all()):
        ts = pd.to_datetime(raw, format="%Y%m%d" if compact else "%Y-%m-%d").dt.tz_localize("UTC")
    else:
        if compact:
            raw = raw + " " + (clock if clock is not None else "000000")
            ts = pd.to_datetime(raw, format="%Y%m%d %H%M%S")
        else:
            ts = pd.to_datetime(raw)
        if ts.dt.tz is None:
            if tz is None:
                raise ValueError("CSV timestamps have no UTC offset; pass the source timezone")
            ts = ts.dt.tz_localize(tz)
    df.index = pd.DatetimeIndex(ts).tz_convert("UTC")
    df.index.name = "ts"
    name = Path(path).name if isinstance(path, (str, Path)) else "upload"
    return Dataset(df[OHLCV].astype(float).sort_index(), DataKind.HISTORICAL, f"csv:{name}")


def synthetic_bars(timeframe: str, periods: int, seed: int, start: str = "2022-01-03", start_price: float = 100.0,
                   annual_vol: float = 0.6, annual_drift: float = 0.05) -> Dataset:
    """Deterministic regime-switching random walk. For tests and UI demos ONLY; always labelled
    SIMULATED and never mixed with market data."""
    rng = np.random.default_rng(seed)
    dt = tf_delta(timeframe) / pd.Timedelta(days=365)
    regime = np.zeros(periods, dtype=int)
    state = 0
    for i in range(periods):
        if rng.random() < 0.01:
            state = int(rng.integers(0, 3))
        regime[i] = state
    drift = np.array([annual_drift, 1.5, -1.5])[regime]
    vol = np.array([annual_vol, annual_vol * 0.6, annual_vol * 1.6])[regime]
    rets = (drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * rng.standard_normal(periods)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.standard_normal((2, periods))) * vol * np.sqrt(dt) * 0.5
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - np.minimum(wick[1], 0.5))
    volume = rng.lognormal(10, 0.5, periods) * (1 + 5 * np.abs(rets) / (vol * np.sqrt(dt)).mean() / 10)
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=periods, freq=tf_delta(timeframe), name="ts")
    bars = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
    return Dataset(bars, DataKind.SIMULATED, f"synthetic:seed={seed}")
