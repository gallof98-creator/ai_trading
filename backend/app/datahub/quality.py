"""Data-quality checks applied to every batch before it is stored.

Same rule as the rest of the platform: bad rows are removed and counted, never repaired or interpolated.
Outliers are different: an extreme but well-formed value may be real, so it is kept and reported.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..market_data.calendars import get_calendar
from ..market_data.quality import validate_bars
from ..market_data.timeframes import OHLCV, tf_delta

OUTLIER_SIGMA = 12.0  # robust z-score of a return beyond which a bar is reported


def robust_outliers(values: pd.Series, sigma: float = OUTLIER_SIGMA) -> pd.Series:
    """True where a value is further than `sigma` robust standard deviations (1.4826 x median absolute deviation) from the median."""
    clean = values.replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 30:
        return pd.Series(False, index=values.index)
    median = clean.median()
    mad = (clean - median).abs().median()
    if not np.isfinite(mad) or mad == 0:
        return pd.Series(False, index=values.index)
    return ((values - median).abs() / (1.4826 * mad) > sigma).fillna(False)


def check_bars(frame: pd.DataFrame, timeframe: str, calendar: str = "24x7") -> tuple[pd.DataFrame, dict]:
    """Validate a batch of bars for one series. Returns (clean rows, report).

    Removed and counted: duplicate timestamps, rows with missing or non-finite prices, impossible bars (high below
    open/close, non-positive low, negative volume). Counted: gaps against the trading calendar, timestamps off the
    timeframe grid (removed), return outliers (kept)."""
    report = {"rows_in": len(frame), "rows_out": 0, "duplicates_removed": 0, "invalid_rows_removed": 0, "off_grid_removed": 0, "missing_bars": 0,
              "missing_examples": [], "outliers": 0, "outlier_examples": [], "first": None, "last": None, "ok": True}
    if frame.empty:
        return frame, report
    delta = tf_delta(timeframe)
    frame = frame.sort_values("ts", kind="stable")
    if delta < pd.Timedelta(days=1):
        off = (frame["ts"].astype("int64") % int(delta / pd.Timedelta(microseconds=1))) != 0
        report["off_grid_removed"] = int(off.sum())
        frame = frame[~off]
    if frame.empty:
        report["ok"] = False
        return frame, report
    indexed = frame.set_index("ts")
    clean, bars_report = validate_bars(indexed[OHLCV], timeframe, get_calendar(calendar))
    kept = frame[~frame["ts"].duplicated(keep="first")]
    kept = kept[kept["ts"].isin(clean.index)].reset_index(drop=True)
    report.update(rows_out=len(kept), duplicates_removed=bars_report.duplicates_removed, invalid_rows_removed=bars_report.invalid_rows_removed,
                  missing_bars=bars_report.missing_bars, missing_examples=bars_report.missing_examples, first=bars_report.first_ts, last=bars_report.last_ts)
    if len(kept) > 1:
        returns = np.log(kept["close"]).diff()
        flagged = robust_outliers(returns)
        report["outliers"] = int(flagged.sum())
        report["outlier_examples"] = [{"ts": kept["ts"].iloc[i].isoformat(), "return": round(float(returns.iloc[i]), 6)} for i in np.flatnonzero(flagged)[:10]]
    report["ok"] = not (report["duplicates_removed"] or report["invalid_rows_removed"] or report["off_grid_removed"] or report["missing_bars"])
    return kept, report


def check_rows(frame: pd.DataFrame, keys: list[str], value: str | None = None, time: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Generic checks for non-bar datasets: duplicate keys and missing values removed and counted; outliers of the value's
    change reported; whether the batch arrived in time order."""
    report = {"rows_in": len(frame), "rows_out": 0, "duplicates_removed": 0, "invalid_rows_removed": 0, "outliers": 0, "was_unsorted": False,
              "first": None, "last": None, "ok": True}
    if frame.empty:
        return frame, report
    duplicated = frame.duplicated(subset=keys, keep="last")
    report["duplicates_removed"] = int(duplicated.sum())
    frame = frame[~duplicated]
    if value is not None:
        bad = frame[value].isna() | ~np.isfinite(pd.to_numeric(frame[value], errors="coerce"))
        report["invalid_rows_removed"] = int(bad.sum())
        frame = frame[~bad]
    if time is not None and len(frame):
        report["was_unsorted"] = not frame[time].is_monotonic_increasing
        report["first"], report["last"] = frame[time].min().isoformat(), frame[time].max().isoformat()
    report["rows_out"] = len(frame)
    report["ok"] = not (report["duplicates_removed"] or report["invalid_rows_removed"])
    return frame.reset_index(drop=True), report


def continuity(stats_first: pd.Timestamp, stats_last: pd.Timestamp, rows: int, timeframe: str, calendar: str = "24x7") -> dict:
    """How complete a stored bar series is over its whole span (for 24x7 markets: rows against the number of periods)."""
    delta = tf_delta(timeframe)
    if calendar != "24x7":
        return {"expected": None, "coverage": None}
    expected = int((stats_last - stats_first) / delta) + 1
    return {"expected": expected, "coverage": round(rows / expected, 6) if expected else None}


def freshness(last: pd.Timestamp | None, cadence_seconds: int | None, now: pd.Timestamp, tolerance: float = 3.0) -> dict:
    """Age of the newest stored event, and whether it is older than `tolerance` times the series' expected cadence."""
    if last is None:
        return {"age_seconds": None, "stale": None}
    age = (now - last).total_seconds()
    return {"age_seconds": round(age, 1), "stale": bool(age > tolerance * cadence_seconds) if cadence_seconds else None}
