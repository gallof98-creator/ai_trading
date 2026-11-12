"""Bar validation and data-quality reporting. Bad rows are removed and counted, never repaired
or interpolated: the platform does not fabricate market data."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from .calendars import SessionCalendar
from .timeframes import OHLCV, epoch_ns, tf_delta


class DataKind(StrEnum):
    """Provenance label carried with every dataset and shown in the UI."""

    REALTIME = "realtime"
    DELAYED = "delayed"
    HISTORICAL = "historical"
    SIMULATED = "simulated"


@dataclass
class DataQualityReport:
    timeframe: str
    rows_in: int = 0
    rows_out: int = 0
    duplicates_removed: int = 0
    invalid_rows_removed: int = 0
    missing_bars: int = 0
    missing_examples: list[str] = field(default_factory=list)
    timezone_converted_from: str | None = None
    was_unsorted: bool = False
    first_ts: str | None = None
    last_ts: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.duplicates_removed or self.invalid_rows_removed or self.missing_bars)

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def normalize_index(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Return a copy indexed in UTC. Naive timestamps are rejected: guessing a timezone silently
    shifts every bar and is one of the classic sources of look-ahead bias."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Bars must be indexed by a DatetimeIndex")
    if df.index.tz is None:
        raise ValueError("Bar timestamps are timezone-naive; state the source timezone explicitly")
    converted = None if str(df.index.tz) == "UTC" else str(df.index.tz)
    out = df.copy()
    out.index = out.index.tz_convert("UTC")
    out.index.name = "ts"
    return out, converted


def expected_index(first: pd.Timestamp, last: pd.Timestamp, timeframe: str, calendar: SessionCalendar | None) -> pd.DatetimeIndex:
    delta = tf_delta(timeframe)
    idx = pd.date_range(first, last, freq=delta)
    if calendar is None or calendar.name == "24x7" or delta >= pd.Timedelta(weeks=1):
        return idx
    if delta >= pd.Timedelta(days=1):
        keep = [calendar.is_trading_day(ts.date()) for ts in idx]
    else:
        keep = [calendar.is_open(ts.to_pydatetime()) for ts in idx]
    return idx[np.array(keep, dtype=bool)]


def validate_bars(df: pd.DataFrame, timeframe: str, calendar: SessionCalendar | None = None) -> tuple[pd.DataFrame, DataQualityReport]:
    missing_cols = [c for c in OHLCV if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing columns: {missing_cols}")
    report = DataQualityReport(timeframe=timeframe, rows_in=len(df))
    out, report.timezone_converted_from = normalize_index(df[OHLCV].astype(float))

    if not out.index.is_monotonic_increasing:
        report.was_unsorted = True
        out = out.sort_index(kind="stable")
    dup = out.index.duplicated(keep="first")
    report.duplicates_removed = int(dup.sum())
    out = out[~dup]

    o, h, l, c, v = (out[k] for k in OHLCV)
    valid = (
        out[OHLCV].notna().all(axis=1) & np.isfinite(out[OHLCV]).all(axis=1)
        & (l > 0) & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c)) & (v >= 0)
    )
    report.invalid_rows_removed = int((~valid).sum())
    out = out[valid]

    if len(out):
        report.first_ts, report.last_ts = out.index[0].isoformat(), out.index[-1].isoformat()
        off_grid = (epoch_ns(out.index) % int(tf_delta(timeframe).total_seconds() * 1e9) != 0) if tf_delta(timeframe) < pd.Timedelta(days=1) else None
        if off_grid is not None and off_grid.any():
            raise ValueError(f"{int(off_grid.sum())} timestamps are not aligned to the {timeframe} grid")
        missing = expected_index(out.index[0], out.index[-1], timeframe, calendar).difference(out.index)
        report.missing_bars = len(missing)
        report.missing_examples = [ts.isoformat() for ts in missing[:10]]
    report.rows_out = len(out)
    return out, report
