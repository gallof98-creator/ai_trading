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


def load_csv(path: str | Path, tz: str | None = None) -> Dataset:
    """CSV with columns timestamp,open,high,low,close,volume. If timestamps carry no UTC offset,
    `tz` (the source timezone) is required."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "timestamp" not in df.columns:
        raise ValueError("CSV needs a 'timestamp' column")
    ts = pd.to_datetime(df.pop("timestamp"))
    if ts.dt.tz is None:
        if tz is None:
            raise ValueError("CSV timestamps have no UTC offset; pass the source timezone")
        ts = ts.dt.tz_localize(tz)
    df.index = pd.DatetimeIndex(ts).tz_convert("UTC")
    df.index.name = "ts"
    return Dataset(df[OHLCV].astype(float), DataKind.HISTORICAL, f"csv:{Path(path).name}")


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
