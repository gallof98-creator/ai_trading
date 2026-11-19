"""Crypto exit study (steps 4 and 5): walk-forward of the crypto strategies before/after the crypto profile, and four exit
rules compared on the same out-of-sample entries.

    cd backend && python -m app.scripts.exit_study --cache /path/to/cache --out report.md [--workers 4]

Data: Alpaca's official crypto bars (no key needed), hourly for the pairs below and daily for the bitcoin regime; cached
as CSV so a rerun uses the same data. Nothing is traded, nothing is sent anywhere but to Alpaca's public data endpoint.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

from ..analytics.report import _max_drawdown
from ..analytics.shadow import simulate_exit
from ..backtesting.walk_forward import WalkForwardSpec
from ..database.settings_store import CryptoProfile, RiskLimits
from ..domain.fees import CommissionModel
from ..domain.models import AssetClass, Instrument
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset
from ..market_data.timeframes import tf_delta
from ..paper_trading.broker import ExecutionModel
from ..quant_ai.opportunities import STOP_GRID, family_of, horizon_of, take_profit_grid, MODULES
from ..quant_ai.parallel import ValidationPool
from ..strategies import indicators as ind
from ..strategies.profile import regime_daily
from ..strategies.registry import create_strategy

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "LINK/USD", "AVAX/USD"]
STRATEGIES = ["zscore_reversion", "rsi_reversion", "bollinger_reversion", "ema_crossover", "volume_breakout"]
TIMEFRAME = "1h"


@dataclass(frozen=True)
class Assumptions:
    """Every number the study rests on, in one place (reported verbatim)."""

    taker_fee: float = 0.0025       # Alpaca crypto, lowest volume tier, taker (to verify against the current fee schedule)
    maker_fee: float = 0.0015       # same, maker
    spread_bps: float = 10.0        # full bid/ask spread assumed on these pairs (half paid per side)
    slippage_bps: float = 5.0       # per side
    usd_eur: float = 0.92           # one fixed rate for the whole period (R figures do not depend on it)
    capital_eur: float = 1000.0
    risk_per_trade: float = 0.02    # 20 EUR per R
    funding_rate_8h: float = 0.0    # spot: no funding (Alpaca has no perpetuals)
    morning: str = "09:00"
    morning_tz: str = "Europe/Rome"
    trailing_atr: float = 2.0       # variant 4: trailing stop on the remaining half, in ATR at entry
    history_days: int = 365
    folds: int = 4


PROFILE_AFTER = dict(enabled=True, regime_filter="trend_only", regime_ema=200, regime_adx_min=25.0, vol_entry=True, vol_entry_atr=1.0,
                     reversion_exit_at_mean=True, time_stop_bars=48, trend_partial=True, partial_fraction=0.5, partial_at_r=1.0,
                     trailing_atr=2.0, breakout_confirm=True, breakout_volume_mult=1.5, breakout_lookback=20, maker_entries=False)


# ---------------------------------------------------------------- data
def fetch_bars(pair: str, timeframe: str, start: datetime, end: datetime, client: httpx.Client) -> pd.DataFrame:
    tf = {"1h": "1Hour", "1d": "1Day"}[timeframe]
    params = {"symbols": pair, "timeframe": tf, "limit": 10_000, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    rows: list[dict] = []
    while True:
        for attempt in range(6):
            r = client.get("https://data.alpaca.markets/v1beta3/crypto/us/bars", params=params)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        body = r.json()
        rows += body.get("bars", {}).get(pair, [])
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame.index = pd.to_datetime(frame["t"], utc=True).rename("ts")
    return frame.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[["open", "high", "low", "close", "volume"]]


def load(pair: str, timeframe: str, days: int, cache: Path, end: datetime) -> pd.DataFrame:
    path = cache / f"{pair.replace('/', '_')}_{timeframe}_{days}d_{end:%Y%m%d}.csv"
    if path.exists():
        return pd.read_csv(path, index_col="ts", parse_dates=["ts"])
    with httpx.Client(timeout=60) as client:
        frame = fetch_bars(pair, timeframe, end - timedelta(days=days), end, client)
    cache.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)
    return frame


def instrument(pair: str) -> Instrument:
    base, quote = pair.split("/")
    return Instrument(id=f"CRYPTO:{pair}", asset_class=AssetClass.CRYPTO, symbol=pair, base_currency=base, quote_currency=quote,
                      calendar="24x7", tick_size=0.01, lot_size=0.00001, min_qty=0.00001)


# ---------------------------------------------------------------- walk-forward before / after
def spec_for(strategy: str, pair: str, bars: pd.DataFrame, a: Assumptions, profile: CryptoProfile | None, regime: pd.Series | None) -> WalkForwardSpec:
    exits = MODULES[horizon_of(TIMEFRAME)]["exits"]
    model = ExecutionModel(spread_bps=a.spread_bps, slippage_bps=a.slippage_bps, participation_rate=None, funding_rate_8h=a.funding_rate_8h,
                           commissions={"crypto": CommissionModel(rate=a.taker_fee, maker_rate=a.maker_fee)})
    return WalkForwardSpec(strategy=strategy, instrument_id=f"CRYPTO:{pair}", timeframe=TIMEFRAME, data=Dataset(bars, DataKind.HISTORICAL, "alpaca-crypto"),
                           param_grid={"stop_atr": sorted({exits["stop_atr"], *STOP_GRID}), "take_profit_atr": take_profit_grid(TIMEFRAME)},
                           initial_capital=a.capital_eur, n_folds=a.folds, fixed_params={"allow_short": False}, base_currency="EUR",
                           execution=model, limits=RiskLimits(risk_per_trade=a.risk_per_trade), static_fx={"USD": a.usd_eur},
                           instruments=[instrument(p) for p in PAIRS], min_trades=3,
                           profile=profile, family=family_of(create_strategy(strategy, {}).category), regime=regime)


def summarise(results: list[dict], a: Assumptions) -> dict:
    """In-sample and out-of-sample figures of one strategy over all pairs and folds, kept apart."""
    is_rows = [f["in_sample"] for r in results for f in r["folds"] if f.get("in_sample")]
    oos_trades = [t for r in results for f in r["folds"] for t in f.get("oos_trades", [])]
    r_oos = [t["r_multiple"] for t in oos_trades if t.get("r_multiple") is not None]
    base = [t["pnl_base"] for t in oos_trades if t.get("pnl_base") is not None]
    wins, losses = [v for v in base if v > 0], [v for v in base if v < 0]
    return {
        "is_trades": sum(x["number_of_trades"] for x in is_rows), "is_mean_return": _mean([x["total_return"] for x in is_rows]),
        "is_mean_pf": _mean([x["profit_factor"] for x in is_rows if x["profit_factor"] is not None and math.isfinite(x["profit_factor"])]),
        "oos_trades": len(oos_trades), "oos_mean_return": _mean([f["out_of_sample"]["total_return"] for r in results for f in r["folds"] if f["out_of_sample"]]),
        "oos_expectancy_r": _mean(r_oos), "oos_expectancy_eur": _mean(base),
        "oos_pf": (sum(wins) / -sum(losses)) if losses else None, "oos_max_dd_eur": _max_drawdown([t["pnl_base"] for t in sorted(oos_trades, key=lambda t: t["exit_ts"]) if t.get("pnl_base") is not None]),
        "oos_cost_eur_per_trade": _mean([(t["commission"] + (t.get("funding_quote") or 0) + (t.get("est_cost_quote") or 0)) * a.usd_eur for t in oos_trades]),
        "combinations": results[0]["combinations_tested"] if results else 0,
    }


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


# ---------------------------------------------------------------- step 4: four exit rules on the same entries
def next_morning(entry: pd.Timestamp, a: Assumptions) -> pd.Timestamp:
    """09:00 local time on the calendar day after the entry's local date."""
    tz = ZoneInfo(a.morning_tz)
    local = entry.tz_convert(tz)
    hh, mm = (int(x) for x in a.morning.split(":"))
    day = (local + pd.Timedelta(days=1)).normalize()
    return pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)).tz_convert("UTC")


def exit_variants(trade: dict, bars: pd.DataFrame, target: pd.Series, a: Assumptions) -> dict[str, tuple[float, pd.Timestamp]] | None:
    """R of one entry under the four rules, after costs. None when the entry cannot be replayed."""
    d = trade["direction"]
    entry_ts, entry = pd.Timestamp(trade["entry_ts"]), trade["entry_price"]
    stop_d = trade.get("initial_stop_distance")
    if not stop_d:
        return None
    params = trade["params"]
    atr = ind.atr(bars, params.get("atr_period", 14))
    before = atr[atr.index < entry_ts]
    if before.empty or not np.isfinite(before.iloc[-1]):
        return None
    a_entry = float(before.iloc[-1])
    stop = entry - d * stop_d
    tp = entry + d * params["take_profit_atr"] * a_entry if params.get("take_profit_atr") else None
    delta = tf_delta(TIMEFRAME)
    after = bars[bars.index >= entry_ts]
    per_side = a.taker_fee + (a.spread_bps / 2 + a.slippage_bps) / 10_000

    def r_of(exit_price: float, gross: bool = False) -> float:
        cost = 0.0 if gross else (entry + exit_price) * per_side
        return ((exit_price - entry) * d - cost) / stop_d

    full = simulate_exit(after, entry_ts, d, stop, tp, None, target, delta, max_days=60)
    if full.reason is None or full.price is None:
        return None
    morning = next_morning(entry_ts, a)
    out = {"1 system exits only": (r_of(full.price), full.ts, r_of(full.price, True))}
    if full.ts is not None and full.ts <= morning:   # the system had already closed it before the morning: same in every variant
        out["2 time stop next morning (all)"] = out["3 winners closed next morning"] = out["4 half next morning + ATR trail"] = out["1 system exits only"]
        return out
    at = after[after.index >= morning]
    if at.empty:
        return None
    m_ts, m_px = at.index[0], float(at["open"].iloc[0])
    out["2 time stop next morning (all)"] = (r_of(m_px), m_ts, r_of(m_px, True))
    if (m_px - entry) * d > 0:
        out["3 winners closed next morning"] = (r_of(m_px), m_ts, r_of(m_px, True))
    else:
        out["3 winners closed next morning"] = out["1 system exits only"]
    rest = simulate_exit(after, m_ts, d, stop, None, a.trailing_atr * a_entry, target, delta, max_days=60)
    if rest.reason is None or rest.price is None:
        return None
    out["4 half next morning + ATR trail"] = (0.5 * r_of(m_px) + 0.5 * r_of(rest.price), max(m_ts, rest.ts),
                                              0.5 * r_of(m_px, True) + 0.5 * r_of(rest.price, True))
    return out


def table_variants(rows: dict[str, list[tuple[float, pd.Timestamp]]], a: Assumptions) -> list[dict]:
    eur_per_r = a.capital_eur * a.risk_per_trade
    out = []
    for name in sorted(rows):
        ordered = sorted(rows[name], key=lambda x: x[1])
        seq = [x[0] for x in ordered]
        gross = [x[2] for x in ordered]
        wins, losses = [r for r in seq if r > 0], [r for r in seq if r < 0]
        out.append({"variant": name, "trades": len(seq), "expectancy_r": _mean(seq), "expectancy_eur": (_mean(seq) or 0) * eur_per_r,
                    "pf": (sum(wins) / -sum(losses)) if losses else None, "max_dd_r": _max_drawdown(seq), "max_dd_eur": _max_drawdown(seq) * eur_per_r,
                    "total_r": sum(seq), "expectancy_gross_r": _mean(gross), "cost_r": (_mean(gross) or 0) - (_mean(seq) or 0),
                    "win_rate": len(wins) / len(seq) if seq else None})
    return out


# ---------------------------------------------------------------- main
def run(cache: Path, workers: int, a: Assumptions) -> dict:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hourly = {p: load(p, "1h", a.history_days, cache, end) for p in PAIRS}
    btc_daily = load("BTC/USD", "1d", a.history_days + 400, cache, end)
    regime = regime_daily(btc_daily, PROFILE_AFTER["regime_ema"], 14, PROFILE_AFTER["regime_adx_min"])
    after_profile = CryptoProfile(**PROFILE_AFTER)
    pool = ValidationPool()
    out: dict = {"pairs": PAIRS, "strategies": STRATEGIES, "timeframe": TIMEFRAME, "assumptions": a.__dict__, "profile_after": PROFILE_AFTER,
                 "data": {p: {"bars": len(b), "from": str(b.index[0]), "to": str(b.index[-1])} for p, b in hourly.items()}, "walk_forward": {}}
    results: dict = {}
    try:
        for label, profile in (("before", None), ("after", after_profile)):
            items = [((label, s, p), spec_for(s, p, hourly[p], a, profile, regime if profile else None)) for s in STRATEGIES for p in PAIRS]
            for (lbl, s, _pair), _spec, res in pool.run_keyed(items, workers):
                if isinstance(res, Exception):
                    results.setdefault((lbl, s), []).append({"error": str(res)})
                    continue
                results.setdefault((lbl, s), []).append(res)
    finally:
        pool.close()
    wf = {}
    for (lbl, s), rs in results.items():
        ok = [r for r in rs if "error" not in r]
        wf.setdefault(s, {})[lbl] = {**summarise(ok, a), "runs": len(ok), "errors": [r["error"] for r in rs if "error" in r][:3]}
    out["walk_forward"] = wf
    out["combinations_tested"] = {s: {"per_run": wf[s]["before"]["combinations"], "runs": len(PAIRS) * 2, "folds": a.folds} for s in wf}
    # Step 4 on the entries of the "before" walk-forward (the strategies as they are), the same entries for every rule.
    variants: dict[str, list] = {}
    per_strategy: dict[str, dict] = {}
    for (lbl, s), rs in results.items():
        if lbl != "before":
            continue
        for r in rs:
            if "error" in r:
                continue
            for f in r["folds"]:
                for t in f.get("oos_trades", []):
                    pair = t["instrument_id"].split(":")[1]
                    bars = hourly[pair]
                    target = create_strategy(s, t["params"]).generate(bars)["target"]
                    v = exit_variants(t, bars, target, a)
                    if v is None:
                        continue
                    for name, val in v.items():
                        variants.setdefault(name, []).append(val)
                        per_strategy.setdefault(s, {}).setdefault(name, []).append(val)
    out["exit_variants"] = table_variants(variants, a)
    out["exit_variants_by_strategy"] = {s: table_variants(v, a) for s, v in per_strategy.items()}
    return out


def markdown(res: dict) -> str:
    a = res["assumptions"]
    f = lambda v, d=2: "—" if v is None else f"{v:.{d}f}"  # noqa: E731
    lines = ["# Crypto exits and profile: study results", "", f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `python -m app.scripts.exit_study`.", "",
             "## Data", "", "| Pair | Hourly bars | From | To |", "|---|---|---|---|"]
    lines += [f"| {p} | {d['bars']} | {d['from'][:16]} | {d['to'][:16]} |" for p, d in res["data"].items()]
    lines += ["", "## Step 4: four exit rules on the same out-of-sample entries", "",
              f"Entries: every out-of-sample trade of the walk-forward of the five strategies on the six pairs (strategies as they are, long-only). "
              f"R after costs; EUR at {a['risk_per_trade']:.0%} of {a['capital_eur']:.0f} EUR per R ({a['capital_eur'] * a['risk_per_trade']:.0f} EUR).", "",
              "| Rule | Trades | Expectancy before costs (R) | Costs (R) | Expectancy (R) | Expectancy (EUR) | Profit factor | Max DD (R) | Total (R) |",
              "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {v['variant']} | {v['trades']} | {f(v['expectancy_gross_r'], 3)} | {f(v['cost_r'], 3)} | {f(v['expectancy_r'], 3)} | {f(v['expectancy_eur'])} | "
              f"{f(v['pf'])} | {f(v['max_dd_r'])} | {f(v['total_r'])} |" for v in res["exit_variants"]]
    lines += ["", "### By strategy", ""]
    for s, rows in sorted(res["exit_variants_by_strategy"].items()):
        lines += [f"**{s}**", "", "| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |", "|---|---|---|---|---|---|"]
        lines += [f"| {v['variant']} | {v['trades']} | {f(v['expectancy_gross_r'], 3)} | {f(v['expectancy_r'], 3)} | {f(v['pf'])} | {f(v['max_dd_r'])} |" for v in rows] + [""]
    lines += ["## Step 5: walk-forward before / after the crypto profile (out of sample apart from in sample)", "",
              "| Strategy | Profile | IS trades | IS mean return | IS mean PF | OOS trades | OOS mean return | OOS expectancy (R) | OOS expectancy (EUR) | OOS PF | OOS max DD (EUR) | Cost / trade (EUR) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s, d in sorted(res["walk_forward"].items()):
        for lbl in ("before", "after"):
            x = d.get(lbl, {})
            lines.append(f"| {s} | {lbl} | {x.get('is_trades', '—')} | {f(x.get('is_mean_return'), 4)} | {f(x.get('is_mean_pf'))} | {x.get('oos_trades', '—')} | "
                         f"{f(x.get('oos_mean_return'), 4)} | {f(x.get('oos_expectancy_r'), 3)} | {f(x.get('oos_expectancy_eur'))} | {f(x.get('oos_pf'))} | "
                         f"{f(x.get('oos_max_dd_eur'))} | {f(x.get('oos_cost_eur_per_trade'))} |")
    lines += ["", "Combinations tested per walk-forward run (each fold re-chooses among them on its training window): "
              + ", ".join(f"{s} {c['per_run']}" for s, c in sorted(res["combinations_tested"].items()))
              + f"; {len(res['pairs'])} pairs x 2 (before/after) runs per strategy, {a['folds']} folds each.", "",
              "Profile used for \"after\" (fixed, not optimised): `" + json.dumps(res["profile_after"]) + "`", "",
              "## Assumptions", ""] + [f"- `{k}` = {v}" for k, v in a.items()]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--json", default="")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    a = Assumptions(history_days=args.days)
    res = run(Path(args.cache), args.workers, a)
    Path(args.out).write_text(markdown(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, default=str, indent=1))
    print(markdown(res))


if __name__ == "__main__":
    main()
