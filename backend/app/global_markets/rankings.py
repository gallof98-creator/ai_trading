"""Transparent quantitative rankings and whole-universe market analysis.

A ranking is a documented formula over the scanner's metrics: each factor is winsorised and
standardised across the eligible universe, signed, weighted and summed. Nothing here is a
prediction: a rank describes where an instrument stands today on stated criteria.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtesting.metrics import DISCLAIMER
from .metrics import FIELDS

EQUITY_LIKE = {"common_stock", "stock", "reit", "adr"}

# model -> description and factors as (metric, direction, weight). Direction +1 = higher is better.
MODELS: dict[str, dict] = {
    "momentum": {"label": "Momentum (12-1)", "description": "Return from twelve months ago to one month ago; the latest month is skipped because very recent winners tend to reverse.",
                 "factors": [("mom_12_1", 1, 1.0)]},
    "relative_strength": {"label": "Relative strength", "description": "Six-month and three-month returns compared with every other eligible instrument.",
                          "factors": [("ret_6m", 1, 0.6), ("ret_3m", 1, 0.4)]},
    "trend_quality": {"label": "Trend quality", "description": "Distance above the 200-day average, trend strength (ADX) and closeness to the 52-week high.",
                      "factors": [("dist_sma_200", 1, 0.4), ("adx_14", 1, 0.3), ("dist_high_52w", 1, 0.3)]},
    "low_volatility": {"label": "Low volatility", "description": "Lower 20-day volatility ranks higher.", "factors": [("volatility_20", -1, 1.0)]},
    "mean_reversion": {"label": "Short-term reversal", "description": "Largest five-day declines rank highest (a bet on a rebound, not on quality).",
                       "factors": [("ret_5d", -1, 1.0)]},
    "breakout": {"label": "Breakout", "description": "Closeness to the 52-week high (60%) and one-month return (40%): instruments pushing to new highs.",
                 "factors": [("dist_high_52w", 1, 0.6), ("ret_1m", 1, 0.4)]},
    "multi_factor": {"label": "Multi-factor", "description": "Momentum 12-1 (40%), low volatility (20%), closeness to the 52-week high (20%) and one-month reversal (20%).",
                     "factors": [("mom_12_1", 1, 0.4), ("volatility_20", -1, 0.2), ("dist_high_52w", 1, 0.2), ("ret_1m", -1, 0.2)]},
}


def describe_models() -> list[dict]:
    return [{"name": name, "label": m["label"], "description": m["description"],
             "factors": [{"metric": f, "label": FIELDS[f][0], "direction": "higher is better" if d > 0 else "lower is better", "weight": w} for f, d, w in m["factors"]]}
            for name, m in MODELS.items()]


def zscores(values: pd.Series) -> pd.Series:
    """Winsorise at the 2.5th / 97.5th percentiles, then standardise. Constant columns give zeros."""
    v = pd.to_numeric(values, errors="coerce")
    lo, hi = v.quantile(0.025), v.quantile(0.975)
    v = v.clip(lo, hi)
    sd = v.std(ddof=0)
    return (v - v.mean()) / sd if sd and sd > 0 else v * 0.0


def eligible(frame: pd.DataFrame, min_price: float = 1.0, min_dollar_volume: float = 1_000_000.0, asset_types: set[str] | None = None,
             markets: list[str] | None = None) -> pd.DataFrame:
    """Instruments a ranking or a cross-sectional strategy may hold: equity-like, liquid enough, real (not simulated) data."""
    if frame.empty:
        return frame
    df = frame[frame["asset_type"].isin(asset_types or EQUITY_LIKE)]
    if markets:
        df = df[df["market"].isin(markets)]
    price, traded = pd.to_numeric(df["close"], errors="coerce"), pd.to_numeric(df["dollar_volume_20"], errors="coerce")
    return df[(price >= min_price) & (traded >= min_dollar_volume)]


def rank(frame: pd.DataFrame, model: str, **eligibility) -> dict:
    if model not in MODELS:
        raise ValueError(f"Unknown ranking model {model!r}; available: {', '.join(MODELS)}")
    spec = MODELS[model]
    universe = eligible(frame, **eligibility)
    factors = [f for f, _, _ in spec["factors"]]
    complete = universe.dropna(subset=factors) if len(universe) else universe
    out = {"model": model, "label": spec["label"], "description": spec["description"], "criteria": describe_models()[list(MODELS).index(model)]["factors"],
           "eligibility": {"min_price": eligibility.get("min_price", 1.0), "min_dollar_volume": eligibility.get("min_dollar_volume", 1_000_000.0),
                           "asset_types": sorted(eligibility.get("asset_types") or EQUITY_LIKE), "markets": eligibility.get("markets") or "all"},
           "universe": int(len(frame)), "eligible": int(len(universe)), "ranked": int(len(complete)), "rows": [],
           "method": "Each factor is winsorised at the 2.5th/97.5th percentile and standardised across the ranked instruments; "
                     "the score is the weighted sum of the signed factor scores. Instruments missing any factor are not ranked.",
           "disclaimer": "A rank states where an instrument stands today on these criteria. It is not a forecast. " + DISCLAIMER}
    if len(complete) < 2:
        return out
    score = pd.Series(0.0, index=complete.index)
    parts = {}
    for f, direction, weight in spec["factors"]:
        parts[f] = zscores(complete[f]) * direction * weight
        score += parts[f]
    ranked = complete.assign(score=score).sort_values("score", ascending=False, kind="stable")
    n = len(ranked)
    for position, (idx, row) in enumerate(ranked.iterrows(), start=1):
        out["rows"].append({"rank": position, "percentile": round(100 * (1 - (position - 1) / n), 1), "instrument_id": row["instrument_id"], "symbol": row["symbol"],
                            "name": row.get("name") or "", "market": row["market"], "sector": row.get("sector"), "score": float(row["score"]),
                            "contributions": {f: float(parts[f].loc[idx]) for f in factors}, "values": {f: float(row[f]) for f in factors},
                            "close": float(row["close"]), "as_of": row["as_of"]})
    return out


def _top(df: pd.DataFrame, column: str, n: int, ascending: bool = False, extra: tuple[str, ...] = ()) -> list[dict]:
    col = pd.to_numeric(df[column], errors="coerce")
    picked = df.assign(_k=col).dropna(subset=["_k"]).sort_values("_k", ascending=ascending).head(n)
    return [{"instrument_id": r["instrument_id"], "symbol": r["symbol"], "name": r.get("name") or "", "market": r["market"], column: float(r["_k"]),
             **{e: (None if pd.isna(r[e]) else r[e]) for e in extra}} for _, r in picked.iterrows()]


def market_overview(frame: pd.DataFrame, group_by: str = "market", markets: list[str] | None = None, n: int = 15) -> dict:
    """What the whole universe looks like today: breadth, groups, movers, unusual activity, regime changes."""
    if group_by not in ("market", "country", "sector"):
        raise ValueError("group_by must be market, country or sector")
    if frame.empty:
        return {"instruments": 0, "note": "No metrics computed yet."}
    df = frame[frame["market"].isin(markets)] if markets else frame
    eq = df[df["asset_type"].isin(EQUITY_LIKE)]
    num = {c: pd.to_numeric(eq[c], errors="coerce") for c in ("change_1d", "dist_sma_50", "dist_sma_200", "dist_high_52w", "ret_1m", "ret_3m", "rel_volume", "atr_ratio")}
    known = lambda s: int(s.notna().sum())  # noqa: E731
    share = lambda mask, base: float(mask.sum() / base) if base else None  # noqa: E731
    breadth = {
        "instruments": int(len(eq)), "advancers": int((num["change_1d"] > 0).sum()), "decliners": int((num["change_1d"] < 0).sum()),
        "unchanged": int((num["change_1d"] == 0).sum()), "median_change_1d": float(num["change_1d"].median()) if known(num["change_1d"]) else None,
        "above_sma_50": share(num["dist_sma_50"] > 0, known(num["dist_sma_50"])), "above_sma_200": share(num["dist_sma_200"] > 0, known(num["dist_sma_200"])),
        "at_52w_high": int((num["dist_high_52w"] >= -0.001).sum()), "breakouts_20d": int(eq["breakout_20"].map(lambda x: x is True).sum()),
        "breakdowns_20d": int(eq["breakdown_20"].map(lambda x: x is True).sum()),
    }
    groups = []
    key = eq[group_by]
    for name, part in eq.groupby(key.where(key.notna(), "(unknown)")):
        r1, r1m, r3m = (pd.to_numeric(part[c], errors="coerce") for c in ("change_1d", "ret_1m", "ret_3m"))
        groups.append({"group": str(name), "instruments": int(len(part)), "avg_change_1d": float(r1.mean()) if r1.notna().any() else None,
                       "avg_ret_1m": float(r1m.mean()) if r1m.notna().any() else None, "avg_ret_3m": float(r3m.mean()) if r3m.notna().any() else None,
                       "advancers_share": share(r1 > 0, int(r1.notna().sum()))})
    groups.sort(key=lambda g: (g["avg_ret_1m"] is None, -(g["avg_ret_1m"] or 0)))
    # Abnormal moves: today's change far outside today's cross-section (robust z from the median and MAD).
    change = num["change_1d"].dropna()
    abnormal = []
    if len(change) >= 20:
        med = change.median()
        mad = (change - med).abs().median()
        scale = 1.4826 * mad if mad > 0 else change.std(ddof=0)
        if scale and scale > 0:
            z = ((change - med) / scale).abs()
            for idx in z[z > 5].sort_values(ascending=False).head(n).index:
                r = eq.loc[idx]
                abnormal.append({"instrument_id": r["instrument_id"], "symbol": r["symbol"], "market": r["market"], "change_1d": float(change.loc[idx]),
                                 "robust_z": float(z.loc[idx])})
    regimes = eq["regime"].dropna()
    changed = eq[eq["regime"].notna() & eq["previous_regime"].notna() & (eq["regime"] != eq["previous_regime"])]
    sector_known = int(eq["sector"].notna().sum())
    return {
        "instruments": int(len(df)), "equities": int(len(eq)), "as_of": str(df["as_of"].max()), "oldest_data": str(df["as_of"].min()),
        "simulated_series": int((df["data_kind"] == "simulated").sum()), "breadth": breadth, "group_by": group_by, "groups": groups,
        "sector_coverage": {"with_sector": sector_known, "without": int(len(eq)) - sector_known},
        "top_gainers": _top(eq, "change_1d", n), "top_losers": _top(eq, "change_1d", n, ascending=True),
        "unusual_volume": _top(eq[num["rel_volume"] >= 2], "rel_volume", n, extra=("change_1d",)),
        "volatility_expansion": _top(eq[num["atr_ratio"] >= 1.25], "atr_ratio", n, extra=("change_1d",)),
        "breakout_candidates": _top(eq[eq["breakout_20"].map(lambda x: x is True)], "rel_volume", n, extra=("change_1d",)),
        "abnormal_moves": abnormal,
        "regimes": {k: int(v) for k, v in regimes.value_counts().items()},
        "regime_changes": [{"instrument_id": r["instrument_id"], "symbol": r["symbol"], "from": r["previous_regime"], "to": r["regime"]}
                           for _, r in changed.head(50).iterrows()],
        "regime_changes_total": int(len(changed)),
        "disclaimer": DISCLAIMER,
    }


def overview_report(overview: dict, ranking: dict | None = None) -> str:
    """Markdown research note from a market overview (and optionally a ranking)."""
    if not overview.get("breadth"):
        return "# Global market report\n\nNo metrics have been computed yet."
    b = overview["breadth"]

    def pct(v):
        return "n/a" if v is None else f"{v:+.2%}"

    def share(v):
        return "n/a" if v is None else f"{v:.0%}"

    lines = ["# Global market report", "", f"> {overview['disclaimer']}", "",
             f"Data as of {overview['as_of']} (oldest series: {overview['oldest_data']}). {overview['equities']} equities analysed"
             + (f"; **{overview['simulated_series']} series are simulated**." if overview["simulated_series"] else "."),
             "", "## Breadth",
             f"- Advancers {b['advancers']}, decliners {b['decliners']}, unchanged {b['unchanged']}; median move {pct(b['median_change_1d'])}.",
             f"- Above the 50-day average: {share(b['above_sma_50'])}; above the 200-day average: {share(b['above_sma_200'])}.",
             f"- At a 52-week high: {b['at_52w_high']}; 20-day breakouts: {b['breakouts_20d']}; 20-day breakdowns: {b['breakdowns_20d']}.",
             "", f"## Performance by {overview['group_by']}", "| Group | Instruments | 1 day | 1 month | 3 months |", "|---|---|---|---|---|"]
    lines += [f"| {g['group']} | {g['instruments']} | {pct(g['avg_change_1d'])} | {pct(g['avg_ret_1m'])} | {pct(g['avg_ret_3m'])} |" for g in overview["groups"][:25]]
    if overview["group_by"] == "sector" and overview["sector_coverage"]["without"]:
        lines.append(f"\n{overview['sector_coverage']['without']} equities have no sector on record and appear as (unknown).")
    for title, key, col, fmt in (("Unusual volume", "unusual_volume", "rel_volume", lambda v: f"{v:.1f}x usual volume"),
                                 ("Volatility expansion", "volatility_expansion", "atr_ratio", lambda v: f"ATR ratio {v:.2f}"),
                                 ("Breakout candidates", "breakout_candidates", "rel_volume", lambda v: f"{v:.1f}x usual volume")):
        lines += ["", f"## {title}"] + ([f"- {r['symbol']} ({r['market']}): {fmt(r[col])}, {pct(r.get('change_1d'))} today" for r in overview[key][:10]] or ["- None today."])
    lines += ["", "## Abnormal price moves"] + ([f"- {r['symbol']} ({r['market']}): {pct(r['change_1d'])}, {r['robust_z']:.1f} robust deviations from today's median"
                                                 for r in overview["abnormal_moves"][:10]] or ["- None today."])
    lines += ["", "## Market regimes", "- " + (", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in overview["regimes"].items()) or "not available"),
              f"- Instruments whose regime changed since the previous computation: {overview['regime_changes_total']}."]
    if ranking and ranking.get("rows"):
        lines += ["", f"## Ranking: {ranking['label']}", ranking["description"], "", ranking["method"], "",
                  f"Ranked {ranking['ranked']} of {ranking['eligible']} eligible instruments.", "", "| Rank | Symbol | Market | Score |", "|---|---|---|---|"]
        lines += [f"| {r['rank']} | {r['symbol']} | {r['market']} | {r['score']:+.2f} |" for r in ranking["rows"][:15]]
        lines += ["", ranking["disclaimer"]]
    return "\n".join(lines)
