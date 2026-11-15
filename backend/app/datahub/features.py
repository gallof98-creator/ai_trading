"""Versioned, reusable quantitative features built from the lake's datasets.

Rules every feature set obeys (and the tests enforce):

* Causal. A value at bar t uses bars up to and including t (known at t's close), never later ones.
* Point in time. Macro and company data are joined on `available_at`, the moment the figure was public,
  not on the period it describes. A revision only shows from the day it was published.
* Versioned. Each set has a name and a version; changing what a column means requires a new version, so a
  stored feature or a research result can always be tied to the definition that produced it.

Nothing here is wired into the trading strategies or the signal engine: these are research inputs.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd

from .lake import DataLake


@dataclass(frozen=True)
class FeatureSet:
    name: str
    version: int
    inputs: tuple[str, ...]
    description: str
    columns: dict[str, str]
    func: Callable[..., pd.DataFrame]

    @property
    def id(self) -> str:
        return f"{self.name}@v{self.version}"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version, "inputs": list(self.inputs), "description": self.description, "columns": self.columns}


REGISTRY: dict[str, FeatureSet] = {}


def feature_set(name: str, version: int, inputs: tuple[str, ...], description: str, columns: dict[str, str]):
    def register(func):
        REGISTRY[name] = FeatureSet(name, version, inputs, description, columns, func)
        return func

    return register


def compute(name: str, *args, **kwargs) -> pd.DataFrame:
    spec = REGISTRY[name]
    out = spec.func(*args, **kwargs).replace([np.inf, -np.inf], np.nan)
    out.attrs["feature_set"] = spec.id
    return out


def describe() -> list[dict]:
    return [REGISTRY[k].to_dict() for k in sorted(REGISTRY)]


# ---------------------------------------------------------------- bars
@feature_set("bars_intraday", 1, ("bars",), "Intraday price and volume features from OHLCV bars of one series.", {
    "ret_1 / ret_5 / ret_15": "log return over 1, 5 and 15 bars",
    "vwap_session": "volume-weighted average price since the start of the session day (the vendor's bar VWAP where present, else the typical price)",
    "vwap_dist": "close relative to vwap_session, minus 1",
    "rvol_tod": "volume divided by the average volume of the same time of day over the previous `rvol_sessions` sessions (needs at least 5 of them)",
    "rvol_roll": "volume divided by the average volume of the previous `rv_window` bars",
    "rv": "realized volatility: square root of the sum of squared 1-bar log returns over `rv_window` bars",
    "rv_ann": "the same, annualised (only when `periods_per_year` is given)",
    "range_bps": "bar high-low range in basis points of the close",
    "bar_gap": "true when the previous bar is further away than the smallest bar spacing seen so far, so returns span a gap"})
def bars_intraday(bars: pd.DataFrame, session_tz: str = "UTC", rv_window: int = 30, rvol_sessions: int = 20, periods_per_year: float | None = None) -> pd.DataFrame:
    close, volume = bars["close"].astype(float), bars["volume"].astype(float)
    log_close = np.log(close)
    out = pd.DataFrame(index=bars.index)
    for n in (1, 5, 15):
        out[f"ret_{n}"] = log_close.diff(n)
    local = bars.index.tz_convert(session_tz)
    session = pd.Series(local.date, index=bars.index)
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    price = bars["vwap"].astype(float).fillna(typical) if "vwap" in bars.columns else typical
    cum_pv, cum_v = (price * volume).groupby(session).cumsum(), volume.groupby(session).cumsum()
    out["vwap_session"] = (cum_pv / cum_v.replace(0, np.nan))
    out["vwap_dist"] = close / out["vwap_session"] - 1
    time_of_day = pd.Series(local.hour * 60 + local.minute, index=bars.index)
    usual = volume.groupby(time_of_day).transform(lambda v: v.shift(1).rolling(rvol_sessions, min_periods=min(5, rvol_sessions)).mean())
    out["rvol_tod"] = volume / usual.replace(0, np.nan)
    out["rvol_roll"] = volume / volume.shift(1).rolling(rv_window, min_periods=rv_window).mean().replace(0, np.nan)
    squared = out["ret_1"] ** 2
    out["rv"] = np.sqrt(squared.rolling(rv_window, min_periods=rv_window).sum())
    if periods_per_year:
        out["rv_ann"] = np.sqrt(squared.rolling(rv_window, min_periods=rv_window).mean() * periods_per_year)
    out["range_bps"] = (bars["high"] - bars["low"]) / close * 1e4
    spacing = bars.index.to_series().diff()
    out["bar_gap"] = (spacing > spacing.cummin()).fillna(False)  # against the smallest spacing seen so far, so it never depends on later bars
    return out


@feature_set("cross_venue", 1, ("bars",), "Price divergence between venues quoting the same asset, on bars with the same open time.", {
    "div_bps_<a>__<b>": "close on venue a relative to close on venue b, minus 1, in basis points",
    "div_z_<a>__<b>": "that divergence standardised over the previous `window` bars"})
def cross_venue(closes: dict[str, pd.Series], window: int = 240) -> pd.DataFrame:
    """`closes` maps a label (e.g. "binance:BTC/USDT") to that series' closes. Only timestamps present on every venue are
    used: a price is never carried forward to fill another venue's gap. Venues quoting in different currencies (USD and
    USDT) differ by that exchange rate as well, which is part of what the divergence shows."""
    panel = pd.concat(closes, axis=1, join="inner").astype(float)
    out = pd.DataFrame(index=panel.index)
    for a, b in combinations(panel.columns, 2):
        divergence = (panel[a] / panel[b] - 1) * 1e4
        out[f"div_bps_{a}__{b}"] = divergence
        mean, sd = divergence.rolling(window, min_periods=window).mean(), divergence.rolling(window, min_periods=window).std()
        out[f"div_z_{a}__{b}"] = (divergence - mean) / sd.replace(0, np.nan)
    return out


@feature_set("cross_asset_correlation", 1, ("bars",), "Rolling correlation of log returns between series.", {
    "corr_<a>__<b>": "correlation of 1-bar log returns over the previous `window` bars, on timestamps both series have"})
def cross_asset_correlation(closes: dict[str, pd.Series], window: int = 120) -> pd.DataFrame:
    returns = np.log(pd.concat(closes, axis=1, join="inner").astype(float)).diff()
    out = pd.DataFrame(index=returns.index)
    for a, b in combinations(returns.columns, 2):
        out[f"corr_{a}__{b}"] = returns[a].rolling(window, min_periods=window).corr(returns[b])
    return out


# ---------------------------------------------------------------- order book and trades
@feature_set("orderbook", 1, ("orderbook",), "Liquidity features from order book snapshots of one venue symbol.", {
    "mid": "midpoint of best bid and ask", "spread_bps": "best ask minus best bid, in basis points of the mid",
    "microprice": "bid and ask weighted by the size on the opposite side",
    "imbalance_1 / imbalance_5 / imbalance_all": "(bid size - ask size) / (bid size + ask size) over the top 1, 5 and all stored levels",
    "depth_bid / depth_ask": "total size over all stored levels"})
def orderbook(snapshots: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=pd.DatetimeIndex(snapshots["ts"], name="ts"))
    bid, ask = snapshots["best_bid"].to_numpy(float), snapshots["best_ask"].to_numpy(float)
    mid = (bid + ask) / 2
    out["mid"], out["spread_bps"] = mid, (ask - bid) / mid * 1e4
    sizes = [(np.asarray(b, float), np.asarray(a, float)) for b, a in zip(snapshots["bid_sz"], snapshots["ask_sz"])]
    top_b, top_a = np.array([b[0] for b, _ in sizes]), np.array([a[0] for _, a in sizes])
    out["microprice"] = (bid * top_a + ask * top_b) / (top_a + top_b)
    for label, levels in (("1", 1), ("5", 5), ("all", None)):
        b, a = np.array([x[:levels].sum() for x, _ in sizes]), np.array([y[:levels].sum() for _, y in sizes])
        out[f"imbalance_{label}"] = (b - a) / (b + a)
    out["depth_bid"], out["depth_ask"] = np.array([b.sum() for b, _ in sizes]), np.array([a.sum() for _, a in sizes])
    return out


@feature_set("trade_flow", 1, ("trades",), "Signed trade flow aggregated into bars.", {
    "volume / buy_volume / sell_volume": "traded size, in total and by the side of the taker", "trades": "number of trades",
    "vwap": "volume-weighted average trade price of the bar",
    "volume_imbalance": "(buy volume - sell volume) / volume, between -1 and 1"})
def trade_flow(trades: pd.DataFrame, timeframe: str = "1m", drop_incomplete: bool = True) -> pd.DataFrame:
    """Bars are labelled by their open time. With `drop_incomplete` the last bar is removed, because trades may still arrive for it."""
    from ..market_data.timeframes import tf_delta

    frame = trades.set_index(pd.DatetimeIndex(trades["ts"], name="ts")).sort_index()
    amount = frame["amount"].astype(float)
    grouper = pd.Grouper(freq=tf_delta(timeframe), label="left", closed="left")
    out = pd.DataFrame({"volume": amount.groupby(grouper).sum(), "buy_volume": amount.where(frame["side"] == "buy", 0.0).groupby(grouper).sum(),
                        "sell_volume": amount.where(frame["side"] == "sell", 0.0).groupby(grouper).sum(), "trades": amount.groupby(grouper).count(),
                        "notional": (amount * frame["price"].astype(float)).groupby(grouper).sum()})
    out = out[out["trades"] > 0]
    out["vwap"] = out.pop("notional") / out["volume"].replace(0, np.nan)
    out["volume_imbalance"] = (out["buy_volume"] - out["sell_volume"]) / out["volume"].replace(0, np.nan)
    return out.iloc[:-1] if drop_incomplete and len(out) else out


# ---------------------------------------------------------------- macro (point in time)
YIELDS = ("DGS3MO", "DGS2", "DGS10", "DGS30")
PRICE_INDEXES = ("CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE")
CURVE = {"yc_10y_2y": ("DGS10", "DGS2"), "yc_10y_3m": ("DGS10", "DGS3MO"), "yc_30y_10y": ("DGS30", "DGS10")}


def _with_release_times(rows: pd.DataFrame, release_times: dict[str, str] | None) -> pd.DataFrame:
    """Optionally replace the conservative 18:00 availability of vintage rows with the series' scheduled New York release time."""
    if not release_times:
        return rows
    rows = rows.copy()
    for series_id, clock in release_times.items():
        mask = (rows["series_id"] == series_id) & (rows["availability"] == "vintage")
        if mask.any():
            day = rows.loc[mask, "realtime_start"].dt.tz_convert("UTC").dt.tz_localize(None)
            rows.loc[mask, "available_at"] = (day + pd.Timedelta(clock + ":00")).dt.tz_localize("America/New_York").dt.tz_convert("UTC").dt.as_unit("us")
    return rows


def latest_known(rows: pd.DataFrame) -> pd.DataFrame:
    """For one series: the value of the most recent observation as it stood after each publication.

    One row per publication moment (`available_at`): the observation date and value of the newest period then
    published. A revision of an older period does not change it; a revision of the newest period does."""
    ordered = rows.sort_values(["available_at", "date"], kind="stable")
    newest = ordered["date"].cummax()
    state = ordered[ordered["date"] == newest]
    return state.drop_duplicates("available_at", keep="last")[["available_at", "date", "value"]].reset_index(drop=True)


def change_known(rows: pd.DataFrame, months: int = 12) -> pd.DataFrame:
    """For one series: the percentage change of the newest observation over `months`, using for both ends the figures
    that were the published ones at that moment (so a later revision of either end is not anticipated)."""
    state = latest_known(rows)
    state["reference"] = state["date"] - pd.DateOffset(months=months)
    candidates = state.merge(rows[["date", "value", "available_at"]], left_on="reference", right_on="date", suffixes=("", "_ref"))
    candidates = candidates[candidates["available_at_ref"] <= candidates["available_at"]]
    base = candidates.sort_values("available_at_ref", kind="stable").groupby("available_at", as_index=False).last()
    base["value"] = base["value"] / base["value_ref"] - 1
    return base[["available_at", "date", "value"]].sort_values("available_at").reset_index(drop=True)


def join_asof(index: pd.DatetimeIndex, events: pd.DataFrame, name: str) -> pd.DataFrame:
    """For every timestamp of `index`: the event value in force (published at or before it), plus how old that publication is."""
    left = pd.DataFrame({"ts": index.as_unit("us")})
    right = events.rename(columns={"value": name}).sort_values("available_at")
    right["available_at"] = right["available_at"].dt.as_unit("us")
    merged = pd.merge_asof(left, right[["available_at", name]], left_on="ts", right_on="available_at", direction="backward")
    out = pd.DataFrame({name: merged[name].to_numpy()}, index=index)
    out[f"{name}_age_days"] = ((merged["ts"] - merged["available_at"]) / pd.Timedelta(days=1)).to_numpy()
    return out


@feature_set("macro", 1, ("macro",), "Macro state as it was known at each timestamp: levels, yield-curve spreads, yield moves and inflation rates.", {
    "<SERIES>": "latest published value of the series", "<SERIES>_age_days": "days since that value was published",
    "yc_10y_2y / yc_10y_3m / yc_30y_10y": "Treasury yield spreads, percentage points",
    "<YIELD>_chg_1 / _chg_5 / _chg_20": "change of a Treasury yield over 1, 5 and 20 observations, percentage points",
    "<PRICE INDEX>_yoy": "12-month change of a price index computed from the figures published at the time"})
def macro(rows: pd.DataFrame, index: pd.DatetimeIndex, release_times: dict[str, str] | None = None) -> pd.DataFrame:
    """`rows` are `macro` dataset rows for any number of series; `index` the timestamps to describe (e.g. a bar index)."""
    rows = _with_release_times(rows, release_times)
    out = pd.DataFrame(index=index)
    for series_id, group in rows.groupby("series_id", sort=True):
        state = latest_known(group)
        out = out.join(join_asof(index, state, series_id))
        if series_id in YIELDS:
            for n in (1, 5, 20):
                moved = state.assign(value=state["value"].diff(n))
                out[f"{series_id}_chg_{n}"] = join_asof(index, moved, "x")["x"]
        if series_id in PRICE_INDEXES:
            out[f"{series_id}_yoy"] = join_asof(index, change_known(group, 12), "x")["x"]
    for name, (long, short) in CURVE.items():
        if long in out.columns and short in out.columns:
            out[name] = out[long] - out[short]
    return out


# ---------------------------------------------------------------- company fundamentals (point in time)
def fundamentals_asof(facts: pd.DataFrame, asof: pd.Timestamp, concepts: list[str] | None = None) -> pd.DataFrame:
    """The figures a company had reported by `asof`: per concept, unit and reporting period, the value from the latest
    filing accepted by then (so a restatement replaces the original only from its own filing time)."""
    known = facts[facts["available_at"] <= asof]
    if concepts:
        known = known[known["concept"].isin(concepts)]
    known = known.sort_values("available_at", kind="stable")
    latest = known.drop_duplicates(["concept", "unit", "period_start", "period_end"], keep="last")
    return latest.sort_values(["concept", "period_end", "period_start"]).reset_index(drop=True)


# ---------------------------------------------------------------- storing computed features
def materialize(lake: DataLake, name: str, frame: pd.DataFrame, source: str, timeframe: str, symbol: str):
    """Store a computed feature frame (indexed by ts) under its versioned id, so it can be read back instead of recomputed."""
    spec = REGISTRY[name]
    rows = frame.reset_index(names="ts")
    rows["feature_set"], rows["source"], rows["timeframe"], rows["symbol"] = spec.id, source, timeframe, symbol
    return lake.write("features", rows)
