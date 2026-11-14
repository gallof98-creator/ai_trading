"""Opportunity engine: which validated strategy fits which instrument now, and the order proposal that follows.

Rules it never breaks:
  * a strategy is proposed for an instrument and timeframe only after a walk-forward validation on that very
    series passed every criterion below. One good backtest is not a validation;
  * a proposal is an order waiting for a human decision. Nothing is sent by this module;
  * every number in an explanation is read from stored bars or from the validation record. There is no
    probability of success, because none was measured.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtesting import service as bt
from ..backtesting.walk_forward import WalkForwardSpec, walk_forward
from ..database.models import ResearchItem, StrategyDeployment
from ..domain.models import TradingMode
from ..market_data.quality import DataKind
from ..market_data.timeframes import tf_delta
from ..strategies.registry import create_strategy, list_strategies
from .regime import detect_regimes

# The nine modules of the specification. Five are signal families (what triggers a trade); four are horizons
# (how long a trade is meant to last), which apply the same signal families on different timeframes and exits.
MODULES: dict[str, dict] = {
    "trend_following": {"kind": "signal", "label": "Trend following", "categories": ["trend", "multi_timeframe"],
                        "fits": ["strong_uptrend", "strong_downtrend"], "description": "Enter in the direction of an established trend."},
    "momentum": {"kind": "signal", "label": "Momentum", "categories": ["momentum"],
                 "fits": ["strong_uptrend", "strong_downtrend", "volatility_expansion"], "description": "Follow price that is accelerating."},
    "mean_reversion": {"kind": "signal", "label": "Mean reversion", "categories": ["mean_reversion"],
                       "fits": ["sideways", "low_volatility", "volatility_contraction", "normal_volatility"],
                       "description": "Fade stretched moves back to an average. Unsuited to strong trends."},
    "breakout": {"kind": "signal", "label": "Breakout", "categories": ["breakout"],
                 "fits": ["volatility_contraction", "volatility_expansion", "strong_uptrend", "strong_downtrend"],
                 "description": "Enter when price leaves a range."},
    "volatility": {"kind": "signal", "label": "Volatility trading", "categories": ["volatility"],
                   "fits": ["volatility_expansion", "volatility_contraction", "high_volatility"], "description": "Trade changes in volatility itself."},
    "scalping": {"kind": "horizon", "label": "Scalping", "timeframes": ["1m", "5m"], "exits": {"stop_atr": 1.0, "take_profit_atr": 1.5},
                 "needs_live": True, "description": "Minutes. Needs a live feed; costs weigh heavily at this horizon."},
    "intraday": {"kind": "horizon", "label": "Intraday", "timeframes": ["15m", "30m", "1h"], "exits": {"stop_atr": 1.5, "take_profit_atr": 3.0},
                 "needs_live": True, "description": "Hours, closed within the day. Needs a live feed."},
    "swing": {"kind": "horizon", "label": "Swing trading", "timeframes": ["4h", "1d"], "exits": {"stop_atr": 2.0, "take_profit_atr": 4.0},
              "needs_live": False, "description": "Days to weeks."},
    "position": {"kind": "horizon", "label": "Position trading", "timeframes": ["1d", "1w"], "exits": {"stop_atr": 3.0, "take_profit_atr": 9.0},
                 "needs_live": False, "description": "Weeks to months."},
}

CRITERIA = {
    "min_folds": 4,                      # independent out-of-sample periods
    "min_profitable_folds": 3,           # at least three of the four must have made money
    "min_out_of_sample_trades": 20,
    "min_out_of_sample_return": 0.0,
    "max_out_of_sample_drawdown": 0.25,
    "min_walk_forward_efficiency": 0.3,  # share of the in-sample result that survived on unseen data
    "valid_for_days": 30,
}
STOP_GRID = [1.5, 2.5]


def horizon_of(timeframe: str) -> str:
    return next(name for name, m in MODULES.items() if m["kind"] == "horizon" and timeframe in m["timeframes"])


def family_of(category: str) -> str:
    return next((name for name, m in MODULES.items() if m["kind"] == "signal" and category in m["categories"]), "trend_following")


def describe_modules() -> dict:
    strategies = list_strategies()
    out = []
    for name, m in MODULES.items():
        entry = {"name": name, **{k: v for k, v in m.items() if k != "categories"}}
        if m["kind"] == "signal":
            entry["strategies"] = [{"name": s["name"], "description": s.get("description", "")} for s in strategies if s["category"] in m["categories"]]
        out.append(entry)
    return {"modules": out, "criteria": CRITERIA,
            "note": "Scalping, intraday, swing and position are horizons: the same signal families run on their timeframes with their own "
                    "stop and target distances. A strategy is proposed only for an instrument and timeframe on which it passed validation."}


def _key(strategy: str, instrument_id: str, timeframe: str) -> str:
    return f"{strategy}|{instrument_id}|{timeframe}"


def _static_fx(rt, instrument_id: str) -> dict[str, float]:
    inst = rt.registry.get(instrument_id)
    if inst.quote_currency == rt.portfolio.base:
        return {}
    return {inst.quote_currency: rt.engine._fx(inst.quote_currency, rt.clock())}


def validate(session: Session, rt, strategy: str, instrument_id: str, timeframe: str, actor: str) -> dict:
    """Walk-forward validation of one strategy on one stored series, judged against CRITERIA and recorded."""
    data = bt.load_dataset(session, instrument_id, timeframe)
    category = create_strategy(strategy, {}).category
    exits = MODULES[horizon_of(timeframe)]["exits"]
    fixed = {"take_profit_atr": exits["take_profit_atr"]}
    capital = rt.user_settings.initial_paper_capital or 1000.0
    result = walk_forward(WalkForwardSpec(
        strategy=strategy, instrument_id=instrument_id, timeframe=timeframe, data=data, param_grid={"stop_atr": sorted({exits["stop_atr"], *STOP_GRID})},
        initial_capital=capital, n_folds=CRITERIA["min_folds"], fixed_params=fixed, base_currency=rt.portfolio.base, limits=rt.user_settings.risk,
        static_fx=_static_fx(rt, instrument_id), instruments=rt.registry.all(), min_trades=3))
    s = result["summary"]
    oos = s.get("out_of_sample_metrics") or {}
    checks = [
        ("market data, not simulated", data.kind is not DataKind.SIMULATED, data.kind.value),
        (f"parameters found in all {CRITERIA['min_folds']} periods", s["folds_with_parameters"] >= CRITERIA["min_folds"], s["folds_with_parameters"]),
        (f"profitable in at least {CRITERIA['min_profitable_folds']} of {CRITERIA['min_folds']} unseen periods",
         s.get("profitable_folds", 0) >= CRITERIA["min_profitable_folds"], s.get("profitable_folds", 0)),
        (f"at least {CRITERIA['min_out_of_sample_trades']} trades on unseen data", oos.get("number_of_trades", 0) >= CRITERIA["min_out_of_sample_trades"],
         oos.get("number_of_trades", 0)),
        ("positive return on unseen data", (oos.get("total_return") or -1) > CRITERIA["min_out_of_sample_return"], oos.get("total_return")),
        (f"drawdown on unseen data at most {CRITERIA['max_out_of_sample_drawdown']:.0%}",
         oos.get("max_drawdown") is not None and oos["max_drawdown"] <= CRITERIA["max_out_of_sample_drawdown"], oos.get("max_drawdown")),
        (f"at least {CRITERIA['min_walk_forward_efficiency']:.0%} of the in-sample result survived",
         (s.get("walk_forward_efficiency") or 0) >= CRITERIA["min_walk_forward_efficiency"], s.get("walk_forward_efficiency")),
    ]
    passed = all(ok for _, ok, _ in checks)
    last = next((f for f in reversed(result["folds"]) if f["best_params"]), None)
    payload = {
        "strategy": strategy, "category": category, "family": family_of(category), "horizon": horizon_of(timeframe), "instrument_id": instrument_id,
        "timeframe": timeframe, "params": last["best_params"] if last else None, "data_kind": data.kind.value, "bars": int(len(data.bars)),
        "data_from": data.bars.index[0].isoformat(), "data_to": data.bars.index[-1].isoformat(),
        "checks": [{"name": n, "passed": bool(ok), "value": v} for n, ok, v in checks],
        "out_of_sample": {k: oos.get(k) for k in ("total_return", "max_drawdown", "sharpe_ratio", "sortino_ratio", "number_of_trades")},
        "profitable_folds": s.get("profitable_folds", 0), "folds": CRITERIA["min_folds"], "walk_forward_efficiency": s.get("walk_forward_efficiency"),
        "combinations_tested": result["combinations_tested"], "tested_by": actor,
        "valid_until": (rt.clock() + timedelta(days=CRITERIA["valid_for_days"])).isoformat(),
        "caveat": "Passing means the rule held up on several unseen periods of this history after costs. It does not mean it will be profitable.",
    }
    row = ResearchItem(id=uuid.uuid4().hex, kind="validation", status="validated" if passed else "rejected", title=_key(strategy, instrument_id, timeframe),
                       created_at=rt.clock(), payload=payload)
    session.add(row)
    return {"id": row.id, "status": row.status, "tested_at": row.created_at.isoformat(), **payload}


def validations(session: Session, now: datetime | None = None) -> list[dict]:
    """The latest validation of every (strategy, instrument, timeframe), newest first."""
    seen, out = set(), []
    for r in session.scalars(select(ResearchItem).where(ResearchItem.kind == "validation").order_by(ResearchItem.created_at.desc())):
        if r.title in seen:
            continue
        seen.add(r.title)
        expired = now is not None and datetime.fromisoformat(r.payload["valid_until"]) < now
        out.append({"id": r.id, "status": "expired" if expired and r.status == "validated" else r.status, "tested_at": r.created_at.isoformat(),
                    "due_for_retest": bool(expired), **r.payload})
    return out


def _recent(last_bar_close: pd.Timestamp, timeframe: str, now: datetime) -> bool:
    """The signal bar must be the latest one a trader could act on: within two bars of now (four days for daily and weekly)."""
    age = pd.Timestamp(now) - last_bar_close
    return age <= max(2 * tf_delta(timeframe), pd.Timedelta(days=4) if tf_delta(timeframe) >= pd.Timedelta(days=1) else pd.Timedelta(0))


def scan(session: Session, rt, broker_id: str, actor: str, instrument_ids: list[str] | None = None, min_traded_value: float = 0.0) -> dict:
    """Look at every validated (strategy, instrument, timeframe): where the strategy opened a position on the latest
    completed bar and the market regime suits it, deploy it (if it is not already) and create a proposal."""
    from ..signals.engine import SignalEngine

    now = rt.clock()
    try:
        adapter = rt.brokers.get(broker_id)
    except Exception:
        raise ValueError(f"Unknown account {broker_id}") from None
    if rt.risk.halted:
        raise ValueError("Trading is halted by the risk engine (kill switch or circuit breaker): no new proposals")
    proposed, skipped = [], []
    for v in validations(session, now):
        iid, tf, strategy = v["instrument_id"], v["timeframe"], v["strategy"]
        if instrument_ids and iid not in instrument_ids:
            continue
        label = {"strategy": strategy, "instrument_id": iid, "timeframe": tf}

        def skip(reason: str) -> None:
            skipped.append({**label, "reason": reason})

        if v["status"] != "validated":
            skip("validation expired; run it again" if v["status"] == "expired" else "did not pass validation")
            continue
        try:
            inst = rt.registry.get(iid)
        except KeyError:
            skip("instrument no longer in the universe")
            continue
        if not rt.registry.is_mapped(broker_id, iid) or inst.asset_class not in adapter.capabilities.asset_classes:
            skip(f"not tradable at {broker_id}")
            continue
        try:
            data = bt.load_dataset(session, iid, tf)
        except bt.DataUnavailable as exc:
            skip(str(exc))
            continue
        if data.kind is DataKind.SIMULATED:
            skip("only simulated prices are stored")
            continue
        bars = data.bars
        instance = create_strategy(strategy, v["params"])
        if len(bars) < instance.warmup() + 50:
            skip("not enough history for a dependable signal")
            continue
        close_time = bars.index[-1] + tf_delta(tf)
        if not _recent(close_time, tf, now):
            skip(f"prices are out of date (last bar closed {close_time:%Y-%m-%d %H:%M} UTC)")
            continue
        if MODULES[v["horizon"]]["needs_live"] and iid not in rt.live.subscribed:
            skip(f"{MODULES[v['horizon']]['label']} needs a live feed for this instrument")
            continue
        traded = float((bars["close"] * bars["volume"]).iloc[-20:].mean())
        if traded < min_traded_value:
            skip(f"average traded value {traded:,.0f} is below the liquidity floor {min_traded_value:,.0f}")
            continue
        regime = str(detect_regimes(bars)["primary"].iloc[-1])
        fits = MODULES[v["family"]]["fits"]
        if regime == "unknown":
            skip("market regime cannot be determined yet")
            continue
        if regime not in fits:
            skip(f"current regime {regime.replace('_', ' ')} does not suit {MODULES[v['family']]['label'].lower()}")
            continue
        signal = SignalEngine().latest(bars, instance, iid, tf)
        if signal is None or signal.action.value == "HOLD" or signal.target == 0:
            skip("no new entry on the latest bar")
            continue
        if signal.target < 0 and not (inst.shortable and adapter.capabilities.supports_short):
            skip("the signal is a short sale, which this account does not allow for the instrument")
            continue
        slice_key = (broker_id, iid, strategy)
        if any((a.order.broker_id, a.order.instrument_id, a.order.strategy) == slice_key for a in rt.engine.approvals.values()):
            skip("already proposed and waiting for a decision")
            continue
        if rt.portfolio.qty(slice_key) != 0:
            skip("already in position")
            continue
        row = session.scalar(select(StrategyDeployment).where(StrategyDeployment.strategy == strategy, StrategyDeployment.instrument_id == iid,
                                                              StrategyDeployment.broker_account_id == broker_id))
        if row is None:
            dep_id = rt.add_deployment(strategy, v["params"], iid, tf, broker_id, actor)
        else:
            dep_id = row.id
        session.expire_all()
        rt.update_deployment(dep_id, actor, active=True, params=v["params"])
        dep = rt.deployments[dep_id]
        rt.bars.setdefault((iid, tf), bars.iloc[-5000:])  # indicators are warm: the next stored or live bar can trade
        signal.regime = regime
        signal.meta.update({
            "opportunity": {"family": v["family"], "horizon": v["horizon"], "regime": regime, "regime_fits": fits, "average_traded_value": traded,
                            "why": f"{strategy} opened a {'long' if signal.target > 0 else 'short'} position at the close of the bar that started "
                                   f"{bars.index[-1]:%Y-%m-%d %H:%M} UTC, at {signal.price:g}; the market regime ({regime.replace('_', ' ')}) is one "
                                   f"that {MODULES[v['family']]['label'].lower()} is meant for."},
            "validation": {"id": v["id"], "tested_at": v["tested_at"], **v["out_of_sample"], "profitable_folds": v["profitable_folds"], "folds": v["folds"],
                           "walk_forward_efficiency": v["walk_forward_efficiency"], "caveat": v["caveat"]},
            "data": {"kind": data.kind.value, "source": data.source, "last_bar_close": close_time.isoformat(), "bars": int(len(bars))}})
        before = set(rt.engine.approvals)
        with rt.lock:
            orders = rt.engine.on_signal(signal, dep, mode_override=TradingMode.SEMI_AUTO)  # a scan never sends anything by itself
            rt.persist()
        new = [a for a in rt.engine.approvals if a not in before]
        if new:
            proposed.append({**label, "proposal_ids": new, "regime": regime})
        else:
            reason = next((o.reason for o in orders if o.status.value == "rejected"), None)
            skip(f"refused by the risk engine: {reason}" if reason else "already proposed for this bar (it was rejected or lapsed), or already in position")
    return {"scanned_at": now.isoformat(), "broker_id": broker_id, "proposed": proposed, "skipped": skipped,
            "validated": sum(1 for v in validations(session, now) if v["status"] == "validated")}


def universes(session: Session, rt, broker_id: str | None = None) -> dict:
    """Three different sets that must not be confused: what is listed, what can be analysed, what can be traded."""
    from sqlalchemy import func

    from ..database.models import BarSeries, Security

    catalog = session.scalar(select(func.count()).select_from(Security).where(Security.listing_status == "active")) or 0
    instruments = rt.registry.all()
    priced = {r.instrument_id for r in session.scalars(select(BarSeries).where(BarSeries.kind != "simulated"))}
    known = {i.id for i in instruments}
    out = {"catalogued": {"count": int(catalog) + sum(1 for i in instruments), "meaning": "Listed securities discovered from providers, plus the platform's own instruments."},
           "analysable": {"count": len(priced & known), "meaning": "Instruments with stored market prices: charts, scanner, strategies and validation work on these."},
           "live": {"count": len(rt.live.subscribed), "meaning": "Instruments receiving live prices right now."},
           "tradable": {}}
    for adapter in rt.brokers.all():
        if broker_id and adapter.broker_id != broker_id:
            continue
        try:
            classes = adapter.capabilities.asset_classes
        except Exception:
            continue
        ids = [i.id for i in instruments if rt.registry.is_mapped(adapter.broker_id, i.id) and i.asset_class in classes]
        out["tradable"][adapter.broker_id] = {"count": len(ids), "with_prices": len(set(ids) & priced), "environment": getattr(adapter, "environment", "paper"),
                                              "meaning": "Instruments this account can actually place orders in."}
    return out
