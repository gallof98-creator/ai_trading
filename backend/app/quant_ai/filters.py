"""Entry filters from company events and fundamentals: the same rules in the validation backtest and in live trading.

Two filters are point in time and therefore part of the validation, so what is tested is what is traded:
  * earnings: no new position in the days around a quarterly results release. Past releases come from the SEC filing
    index (an 8-K with item 2.02 "Results of Operations", or a 10-Q/10-K when a company files no such 8-K). The next
    release is expected one usual interval after the last one; the window before it uses only releases already known.
  * fundamentals: long entries only in companies whose latest annual report, as published at the time, shows a profit,
    positive equity, revenue not shrinking fast, and liabilities not far above equity.
A third, news, is applied in live trading only: no new position shortly after a news item about the instrument. Its
history is not downloaded for the validation, so it is the one filter that was not tested.

Filters only ever remove entries. They never open, enlarge or keep a position, and they never block an exit.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from ..database.settings_store import FilterSettings  # noqa: F401  (defined with the other settings)

REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
CONCEPTS = [*REVENUE, "NetIncomeLoss", "StockholdersEquity", "Liabilities"]
ANNUAL_DAYS = (330, 400)  # a fiscal year in a 10-K, with 52/53-week years


# ---------------------------------------------------------------- applying a block to a strategy's signals
def apply_entry_block(signals: pd.DataFrame, blocked_any: pd.Series | None = None, blocked_long: pd.Series | None = None) -> pd.DataFrame:
    """Remove every position whose opening bar is blocked: the strategy stays flat until it would next open one.
    (Entering late, when the block lifts, would trade a different rule.) `blocked_*` are booleans by bar start time."""
    if (blocked_any is None or not blocked_any.any()) and (blocked_long is None or not blocked_long.any()):
        return signals
    target = signals["target"].to_numpy(copy=True)
    idx = signals.index
    any_ = blocked_any.reindex(idx, fill_value=False).to_numpy(bool) if blocked_any is not None else np.zeros(len(idx), bool)
    long_ = blocked_long.reindex(idx, fill_value=False).to_numpy(bool) if blocked_long is not None else np.zeros(len(idx), bool)
    k = 0
    while k < len(target):
        if target[k] == 0:
            k += 1
            continue
        end = k
        while end + 1 < len(target) and target[end + 1] == target[k]:
            end += 1
        if any_[k] or (target[k] > 0 and long_[k]):
            target[k:end + 1] = 0
        k = end + 1
    out = signals.copy()
    out["target"] = target
    change = out["target"].diff().fillna(out["target"])
    out["action"] = np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD")
    return out


def bar_close_times(index: pd.DatetimeIndex, timeframe: str) -> pd.DatetimeIndex:
    from ..market_data.timeframes import tf_delta

    return index + tf_delta(timeframe)


# ---------------------------------------------------------------- earnings
def releases_from_filings(filings: pd.DataFrame) -> list[pd.Timestamp]:
    """Results releases: 8-Ks with item 2.02, or the periodic reports when a company files no such 8-K."""
    if filings is None or not len(filings):
        return []
    items = filings["items"].fillna("").astype(str)
    eight_k = filings[filings["form"].isin(["8-K", "8-K/A"]) & items.str.contains("2.02", regex=False)]
    chosen = eight_k if len(eight_k) else filings[filings["form"].isin(["10-Q", "10-K"])]
    ts = pd.to_datetime(chosen["available_at"], utc=True)
    out = sorted(set(ts.dropna()))
    # One release per quarter: an 8-K/A or a second filing a few days later is the same release.
    merged: list[pd.Timestamp] = []
    for t in out:
        if not merged or t - merged[-1] > pd.Timedelta(days=20):
            merged.append(t)
    return merged


def earnings_windows(releases: list[pd.Timestamp], cfg: FilterSettings) -> list[tuple[pd.Timestamp, pd.Timestamp, str]]:
    """(start, end, reason) intervals with no new entries. Each window is built from releases known at its start."""
    out = []
    after = pd.Timedelta(hours=cfg.earnings_hours_after)
    for k, r in enumerate(releases):
        if after > pd.Timedelta(0):
            out.append((r, r + after, f"quarterly results released {r:%Y-%m-%d %H:%M} UTC"))
        gaps = [releases[j] - releases[j - 1] for j in range(max(1, k - 3), k + 1)]
        gaps = [g for g in gaps if pd.Timedelta(days=60) <= g <= pd.Timedelta(days=130)]
        gap = pd.Series(gaps).median() if gaps else pd.Timedelta(days=91)
        expected = r + gap
        start = expected - pd.Timedelta(days=cfg.earnings_days_before)
        end = expected + pd.Timedelta(days=5)  # results are often a few days later than the usual interval
        if k + 1 < len(releases):
            end = min(end, releases[k + 1])  # the actual release ends the wait (and starts its own window)
        if cfg.earnings_days_before and end > start:
            out.append((start, end, f"quarterly results expected around {expected:%Y-%m-%d}"))
    return out


def blocked_by_windows(times: pd.DatetimeIndex, windows: list[tuple[pd.Timestamp, pd.Timestamp, str]]) -> np.ndarray:
    mask = np.zeros(len(times), bool)
    values = times.as_unit("ns").asi8 if len(times) else np.array([], dtype=np.int64)  # Timestamp.value is always in ns
    for start, end, _ in windows:
        mask |= (values >= start.value) & (values < end.value)
    return mask


def window_at(ts: pd.Timestamp, windows) -> str | None:
    return next((reason for start, end, reason in windows if start <= ts < end), None)


# ---------------------------------------------------------------- fundamentals
@dataclass
class Snapshot:
    asof: pd.Timestamp
    net_income: float | None
    revenue: float | None
    revenue_growth: float | None
    equity: float | None
    liabilities: float | None

    @property
    def margin(self) -> float | None:
        return self.net_income / self.revenue if self.net_income is not None and self.revenue else None

    @property
    def roe(self) -> float | None:
        return self.net_income / self.equity if self.net_income is not None and self.equity and self.equity > 0 else None

    @property
    def leverage(self) -> float | None:
        return self.liabilities / self.equity if self.liabilities is not None and self.equity and self.equity > 0 else None

    def score(self) -> float | None:
        parts = [v for v in (self.revenue_growth, self.margin, self.roe) if v is not None]
        return float(np.mean([np.clip(v, -1, 1) for v in parts])) if parts else None

    def failures(self, cfg: FilterSettings) -> list[str]:
        out = []
        if self.net_income is not None and self.net_income <= 0:
            out.append(f"loss in the last fiscal year ({self.net_income:,.0f})")
        if self.equity is not None and self.equity <= 0:
            out.append("negative shareholders' equity")
        if self.revenue_growth is not None and self.revenue_growth < cfg.min_revenue_growth:
            out.append(f"revenue down {-self.revenue_growth:.0%} on the year")
        if self.leverage is not None and self.leverage > cfg.max_liabilities_to_equity:
            out.append(f"liabilities {self.leverage:.1f} times equity")
        return out

    def to_dict(self) -> dict:
        return {"asof": self.asof.isoformat(), "net_income": self.net_income, "revenue": self.revenue, "revenue_growth": self.revenue_growth,
                "equity": self.equity, "liabilities": self.liabilities, "margin": self.margin, "roe": self.roe, "liabilities_to_equity": self.leverage,
                "score": self.score()}


def _latest(known: pd.DataFrame, concept: str, annual: bool) -> pd.DataFrame:
    rows = known[known["concept"] == concept]
    if annual:
        rows = rows[rows["duration_days"].between(*ANNUAL_DAYS)]
    else:
        rows = rows[rows["duration_days"] <= 1]
    # The value from the latest filing for each period: a restatement replaces the original from its own filing time.
    return rows.sort_values("available_at", kind="stable").drop_duplicates(["period_end"], keep="last").sort_values("period_end")


def snapshot(facts: pd.DataFrame, asof: pd.Timestamp) -> Snapshot:
    """What the company's reports said, as known at `asof`."""
    known = facts[facts["available_at"] <= asof] if len(facts) else facts
    ni = _latest(known, "NetIncomeLoss", annual=True) if len(known) else known
    revenue, growth = None, None
    for concept in REVENUE:
        rev = _latest(known, concept, annual=True) if len(known) else known
        if len(rev):
            revenue = float(rev["value"].iloc[-1])
            prior = rev[rev["period_end"] <= rev["period_end"].iloc[-1] - pd.Timedelta(days=330)]
            if len(prior) and prior["value"].iloc[-1] > 0:
                growth = revenue / float(prior["value"].iloc[-1]) - 1
            break
    eq = _latest(known, "StockholdersEquity", annual=False) if len(known) else known
    li = _latest(known, "Liabilities", annual=False) if len(known) else known
    return Snapshot(asof, float(ni["value"].iloc[-1]) if len(ni) else None, revenue, growth,
                    float(eq["value"].iloc[-1]) if len(eq) else None, float(li["value"].iloc[-1]) if len(li) else None)


def fundamentals_blocked(facts: pd.DataFrame, times: pd.DatetimeIndex, cfg: FilterSettings) -> np.ndarray:
    """For each time, whether a long entry is refused on the reports known then. No reports yet: not refused."""
    mask = np.zeros(len(times), bool)
    if facts is None or not len(facts) or not len(times):
        return mask
    changes = sorted(set(pd.to_datetime(facts["available_at"], utc=True)))
    values = times.as_unit("ns").asi8  # Timestamp.value is always in ns
    for k, t in enumerate(changes):
        end = changes[k + 1] if k + 1 < len(changes) else None
        sel = (values >= t.value) & ((values < end.value) if end is not None else True)
        if sel.any() and snapshot(facts, t).failures(cfg):
            mask |= sel
    return mask


def facts_frame(rows: list[list]) -> pd.DataFrame:
    """The compact stored form (see company_data) back into a frame."""
    frame = pd.DataFrame(rows, columns=["concept", "period_start", "period_end", "value", "available_at"])
    for c in ("period_start", "period_end", "available_at"):
        frame[c] = pd.to_datetime(frame[c], utc=True)
    frame["duration_days"] = (frame["period_end"] - frame["period_start"]).dt.days
    return frame
