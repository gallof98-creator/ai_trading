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
from ..backtesting.stats import benjamini_hochberg, expected_max_sharpe
from ..backtesting.walk_forward import WalkForwardSpec, walk_forward
from ..brokers.base import is_paper_account
from ..database.models import ResearchItem, StrategyDeployment
from ..domain.models import SignalAction, TradingMode
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
    "version": 3,  # 3: the take profit is chosen by the validation, among TAKE_PROFIT_GRID
    "min_folds": 4,                      # independent out-of-sample periods
    "min_profitable_folds": 3,           # at least three of the four must have made money
    "min_out_of_sample_trades": 20,
    "min_out_of_sample_return": 0.0,
    "max_out_of_sample_drawdown": 0.25,
    "min_walk_forward_efficiency": 0.3,  # share of the in-sample result that survived on unseen data
    # Statistical significance: the chance that a strategy with no edge shows this Sharpe ratio on this many unseen days.
    "max_p_value": 0.05,
    # Robustness: still profitable with trading costs doubled; most parameter settings profitable on unseen data, not
    # just the chosen one; and still profitable without its single best trade.
    "stress_cost_multiplier": 2.0,
    "min_profitable_parameter_share": 0.6,
    # Multiple testing: across every setup tested, the expected share of false discoveries among the validated ones.
    "max_false_discovery_rate": 0.10,
    "valid_for_days": 30,
}
STOP_GRID = [1.5, 2.5]
# The take profit is chosen by the validation, like the stop: a nearer and a farther target around the horizon's own,
# and none at all (the position is closed only by the strategy's exit or the stop). Multiples of the horizon's target.
TAKE_PROFIT_GRID = [0.5, 1.0, 1.5, None]


def take_profit_grid(timeframe: str) -> list[float | None]:
    base = MODULES[horizon_of(timeframe)]["exits"]["take_profit_atr"]
    return [round(base * m, 2) if m is not None else None for m in TAKE_PROFIT_GRID]
CANDIDATE = {"min_profitable_folds": 3, "min_out_of_sample_trades": 8}
# The third, weakest level: setups that failed validation but did not lose on unseen data. Traded only when the user
# switches exploration on, only on paper accounts, to measure them in real time; never evidence of an edge.
EXPLORATION = {"min_profitable_folds": 2, "min_out_of_sample_trades": 5}
LEVELS = ("validated", "candidate", "exploration")
# How long after its signal bar an entry may still be proposed, and how far the price may have moved by then.
SCAN = {"entry_window_bars": 3, "max_drift_stop_fraction": 0.5}
SCAN_BARS = 6000  # bars read for a signal: every daily history fits; an intraday one is read from its recent end


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
    return {"modules": out, "criteria": CRITERIA, "take_profit_grid": {h: take_profit_grid(MODULES[h]["timeframes"][0]) for h, m in MODULES.items() if m["kind"] == "horizon"},
            "note": "Scalping, intraday, swing and position are horizons: the same signal families run on their timeframes with their own "
                    "stop and target distances. A strategy is proposed only for an instrument and timeframe on which it passed validation."}


def _key(strategy: str, instrument_id: str, timeframe: str) -> str:
    return f"{strategy}|{instrument_id}|{timeframe}"


def _static_fx(rt, instrument_id: str) -> dict[str, float]:
    inst = rt.registry.get(instrument_id)
    if inst.quote_currency == rt.portfolio.base:
        return {}
    return {inst.quote_currency: rt.engine._fx(inst.quote_currency, rt.clock())}


def prepare(session: Session, rt, strategy: str, instrument_id: str, timeframe: str, allow_short: bool = False, max_bars: int | None = None,
            data=None, always_take_profit: bool = False) -> WalkForwardSpec:
    """Everything a validation needs, read from the database and the runtime. The result is self-contained, so the
    computation itself (`walk_forward`) can run in another process."""
    if data is None:
        data = bt.load_dataset(session, instrument_id, timeframe, last=max_bars or bt.MAX_BARS)
    if max_bars and len(data.bars) > max_bars:
        from ..market_data.sources import Dataset

        data = Dataset(data.bars.iloc[-max_bars:], data.kind, data.source)
    create_strategy(strategy, {})  # an unknown strategy fails here, before any work is queued
    exits = MODULES[horizon_of(timeframe)]["exits"]
    fixed = {"allow_short": bool(allow_short)}
    capital = rt.user_settings.initial_paper_capital or 1000.0
    company = getattr(rt, "company", None)
    blocked_any, blocked_long, filters = company.masks(instrument_id, timeframe, data.bars.index) if company else (None, None, {})
    ctx = rt.profile_context(instrument_id, create_strategy(strategy, {})) if hasattr(rt, "profile_context") else None
    profile, family, regime = ctx if ctx else (None, None, None)
    return WalkForwardSpec(
        blocked_any=blocked_any, blocked_long=blocked_long, filters=filters, profile=profile, family=family, regime=regime,
        strategy=strategy, instrument_id=instrument_id, timeframe=timeframe, data=data, param_grid={"stop_atr": sorted({exits["stop_atr"], *STOP_GRID}),
                                                                                                 "take_profit_atr": [t for t in take_profit_grid(timeframe)
                                                                                                                     if t is not None or not always_take_profit]},
        initial_capital=capital, n_folds=CRITERIA["min_folds"], fixed_params=fixed, base_currency=rt.portfolio.base, limits=rt.user_settings.risk,
        static_fx=_static_fx(rt, instrument_id), instruments=rt.registry.all(), min_trades=3, robustness=True,
        stress_costs=CRITERIA["stress_cost_multiplier"])


def judge(spec: WalkForwardSpec, result: dict, actor: str, now: datetime) -> tuple[str, dict]:
    """(level, record) for a finished walk-forward: validated, candidate or rejected, with every check and its value."""
    data, strategy, instrument_id, timeframe = spec.data, spec.strategy, spec.instrument_id, spec.timeframe
    category = create_strategy(strategy, {}).category
    s = result["summary"]
    oos = s.get("out_of_sample_metrics") or {}
    stats = s.get("out_of_sample_statistics") or {}
    robust = s.get("robustness") or {}
    p = stats.get("p_value")
    costly, share, without_best = robust.get("return_with_stressed_costs"), robust.get("profitable_parameter_share"), robust.get("net_profit_without_best_trade")
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
        (f"statistically significant: at most {CRITERIA['max_p_value']:.0%} chance of this result without any edge",
         p is not None and p <= CRITERIA["max_p_value"], p),
        (f"still profitable with trading costs multiplied by {CRITERIA['stress_cost_multiplier']:g}", costly is not None and costly > 0, costly),
        (f"at least {CRITERIA['min_profitable_parameter_share']:.0%} of the parameter settings profitable on unseen data",
         share is not None and share >= CRITERIA["min_profitable_parameter_share"], share),
        ("still profitable without its best trade", without_best is not None and without_best > 0, without_best),
    ]
    passed = all(ok for _, ok, _ in checks)
    # A second, weaker level. Daily strategies trade a handful of times a year on one instrument, so twenty unseen trades and
    # statistical significance are rarely available; "candidate" keeps the requirements that do not depend on sample size
    # and is allowed on paper accounts only, where trading it is itself the next stage of evidence.
    candidate_checks = [
        ("market data, not simulated", data.kind is not DataKind.SIMULATED),
        (f"profitable in at least {CANDIDATE['min_profitable_folds']} of {CRITERIA['min_folds']} unseen periods", s.get("profitable_folds", 0) >= CANDIDATE["min_profitable_folds"]),
        (f"at least {CANDIDATE['min_out_of_sample_trades']} trades on unseen data", oos.get("number_of_trades", 0) >= CANDIDATE["min_out_of_sample_trades"]),
        ("positive return on unseen data", (oos.get("total_return") or -1) > 0),
        (f"drawdown on unseen data at most {CRITERIA['max_out_of_sample_drawdown']:.0%}",
         oos.get("max_drawdown") is not None and oos["max_drawdown"] <= CRITERIA["max_out_of_sample_drawdown"]),
        (f"still profitable with trading costs multiplied by {CRITERIA['stress_cost_multiplier']:g}", costly is not None and costly > 0),
        ("still profitable without its best trade", without_best is not None and without_best > 0),
    ]
    tier = "validated" if passed else "candidate" if all(ok for _, ok in candidate_checks) else "rejected"
    last = next((f for f in reversed(result["folds"]) if f["best_params"]), None)
    payload = {
        "criteria_version": CRITERIA["version"],
        "strategy": strategy, "category": category, "family": family_of(category), "horizon": horizon_of(timeframe), "instrument_id": instrument_id,
        "timeframe": timeframe, "params": last["best_params"] if last else None, "filters": dict(getattr(spec, "filters", {}) or {}),
        "profile": spec.profile.signature() if getattr(spec, "profile", None) is not None else None, "data_kind": data.kind.value, "data_source": data.source,
        "bars": int(len(data.bars)),
        "data_from": data.bars.index[0].isoformat(), "data_to": data.bars.index[-1].isoformat(), "long_only": not spec.fixed_params.get("allow_short"),
        "checks": [{"name": n, "passed": bool(ok), "value": v} for n, ok, v in checks],
        "candidate_checks": [{"name": n, "passed": bool(ok)} for n, ok in candidate_checks],
        "out_of_sample": {k: oos.get(k) for k in ("total_return", "max_drawdown", "sharpe_ratio", "sortino_ratio", "number_of_trades")},
        "statistics": stats, "robustness": robust,
        "profitable_folds": s.get("profitable_folds", 0), "folds": CRITERIA["min_folds"], "walk_forward_efficiency": s.get("walk_forward_efficiency"),
        "combinations_tested": result["combinations_tested"], "tested_by": actor,
        "valid_until": (now + timedelta(days=CRITERIA["valid_for_days"])).isoformat(),
        "caveat": ("Passing means the rule held up on several unseen periods of this history after costs, with a result unlikely to be "
                   "luck. It does not mean it will be profitable."
                   if tier != "candidate" else
                   "Candidate: profitable on most unseen periods, but without enough evidence to count as validated. When many strategy/instrument "
                   "pairs are tested, some reach this level by chance. Paper accounts only."),
    }
    return tier, payload


def record(session: Session, tier: str, payload: dict, now: datetime) -> dict:
    row = ResearchItem(id=uuid.uuid4().hex, kind="validation", status=tier, title=_key(payload["strategy"], payload["instrument_id"], payload["timeframe"]),
                       created_at=now, payload=payload)
    session.add(row)
    return {"id": row.id, "status": row.status, "tested_at": row.created_at.isoformat(), **payload}


def validate(session: Session, rt, strategy: str, instrument_id: str, timeframe: str, actor: str, allow_short: bool = False,
             max_bars: int | None = None) -> dict:
    """Walk-forward validation of one strategy on one stored series, judged against CRITERIA and recorded.
    Long-only unless `allow_short`: a strategy is validated the way it will be traded, and a small cash account cannot sell short.
    `max_bars` limits the history to the most recent bars (older markets say less about today, and the test runs faster)."""
    spec = prepare(session, rt, strategy, instrument_id, timeframe, allow_short, max_bars)
    tier, payload = judge(spec, walk_forward(spec), actor, rt.clock())
    return record(session, tier, payload, rt.clock())


_CACHE: dict = {}


def validations(session: Session, now: datetime | None = None) -> list[dict]:
    """See `_validations`. Every page refresh and every scan reads them, and the table only grows (each retest adds a
    row), so the result is kept until a validation is recorded or the minute changes (expiry depends on the time)."""
    from sqlalchemy import func

    count, latest = session.execute(select(func.count(), func.max(ResearchItem.created_at)).where(ResearchItem.kind == "validation")).one()
    key = (str(session.get_bind().url), count, latest, now.replace(second=0, microsecond=0) if now else None, CRITERIA["version"])
    hit = _CACHE.get("validations")
    if hit is None or hit[0] != key:
        hit = (key, _validations(session, now))
        _CACHE["validations"] = hit
    return [dict(v) for v in hit[1]]  # top-level copies: callers may set fields without changing the cached result


def _validations(session: Session, now: datetime | None = None) -> list[dict]:
    """The latest validation of every (strategy, instrument, timeframe), newest first.

    Two rules are applied here, on the whole set, because they depend on the other tests:
      * a result recorded under older, weaker criteria is due for a new test and is not traded meanwhile;
      * multiple testing: among all the setups tested, only those that survive the false-discovery-rate control stay
        "validated". One that passed on its own but not across the family is reported as a candidate, with the reason."""
    from sqlalchemy import and_, func

    # Only the latest test of each setup is read (every retest adds a row; the history is kept, not re-read).
    latest = (select(ResearchItem.title, func.max(ResearchItem.created_at).label("at")).where(ResearchItem.kind == "validation")
              .group_by(ResearchItem.title).subquery())
    query = (select(ResearchItem).join(latest, and_(ResearchItem.title == latest.c.title, ResearchItem.created_at == latest.c.at))
             .where(ResearchItem.kind == "validation").order_by(ResearchItem.created_at.desc(), ResearchItem.id.desc()))
    seen, out = set(), []
    for r in session.scalars(query):
        if r.title in seen:
            continue
        seen.add(r.title)
        expired = now is not None and datetime.fromisoformat(r.payload["valid_until"]) < now
        outdated = r.payload.get("criteria_version") != CRITERIA["version"] and r.status != "rejected"
        out.append({"id": r.id, "status": "expired" if (expired and r.status == "validated") or outdated else r.status, "tested_at": r.created_at.isoformat(),
                    "due_for_retest": bool(expired or outdated), "recorded_status": r.status, **r.payload})
    family = {v["id"]: (v.get("statistics") or {}).get("p_value") for v in out if v.get("criteria_version") == CRITERIA["version"] and not v["due_for_retest"]}
    accepted = benjamini_hochberg(family, CRITERIA["max_false_discovery_rate"])
    for v in out:
        if v["status"] == "validated" and v["id"] not in accepted:
            v["status"] = "candidate"
            v["caveat"] = (f"Passed every check on its own, but not after correcting for the {len(family)} setups tested: with that many tests "
                           "some pass by chance. Treated as a candidate (paper accounts only) until the evidence is stronger.")
    return out


def multiple_testing(session: Session, now: datetime | None = None) -> dict:
    """How many setups were tested and what the best of that many worthless ones would be expected to show."""
    tested = [v for v in validations(session, now) if v.get("criteria_version") == CRITERIA["version"] and not v["due_for_retest"]]
    sharpes = [s for s in ((v.get("statistics") or {}).get("sharpe_per_period") for v in tested) if s is not None]
    spread = float(pd.Series(sharpes).std(ddof=1)) if len(sharpes) > 1 else 0.0
    chance = expected_max_sharpe(len(sharpes), spread)
    return {"setups_tested": len(tested), "validated": sum(v["status"] == "validated" for v in tested),
            "candidates": sum(v["status"] == "candidate" for v in tested), "false_discovery_rate": CRITERIA["max_false_discovery_rate"],
            "best_daily_sharpe_expected_by_chance": chance, "best_annual_sharpe_expected_by_chance": chance * 252 ** 0.5,
            "meaning": "With this many setups tested, the best of them would be expected to reach this Sharpe ratio on unseen data even if "
                       "none had an edge. A validated setup has to be unlikely as a fluke among all of them, not just on its own."}


def explorable(v: dict) -> bool:
    """A rejected setup that is not clearly losing: market data, enough unseen trades, positive unseen return after
    costs, drawdown within the limit, profitable in at least half the unseen periods. Results under current criteria only."""
    oos = v.get("out_of_sample") or {}
    return (v.get("status") == "rejected" and v.get("criteria_version") == CRITERIA["version"] and not v.get("due_for_retest")
            and v.get("data_kind") != DataKind.SIMULATED.value
            and (oos.get("number_of_trades") or 0) >= EXPLORATION["min_out_of_sample_trades"]
            and (oos.get("total_return") or 0) > 0
            and oos.get("max_drawdown") is not None and oos["max_drawdown"] <= CRITERIA["max_out_of_sample_drawdown"]
            and (v.get("profitable_folds") or 0) >= EXPLORATION["min_profitable_folds"])


def level_of(v: dict) -> str | None:
    """The level a setup may be traded at: validated, candidate, exploration, or None."""
    if v["status"] in ("validated", "candidate"):
        return v["status"]
    return "exploration" if explorable(v) else None


def _recent(last_bar_close: pd.Timestamp, timeframe: str, now: datetime) -> bool:
    """The signal bar must be the latest one a trader could act on: within two bars of now (four days for daily and weekly)."""
    age = pd.Timestamp(now) - last_bar_close
    return age <= max(2 * tf_delta(timeframe), pd.Timedelta(days=4) if tf_delta(timeframe) >= pd.Timedelta(days=1) else pd.Timedelta(0))


def scan(session: Session, rt, broker_id: str, actor: str, instrument_ids: list[str] | None = None, min_traded_value: float = 0.0,
         include_candidates: bool = False, require_regime_fit: bool = False, include_exploration: bool = False,
         always_take_profit: bool = False) -> dict:
    """Look at every validated (strategy, instrument, timeframe): where the strategy opened a position on the latest
    completed bar and the market regime suits it, deploy it (if it is not already) and create a proposal.

    Weaker evidence is traded on paper accounts only (the internal one, or a broker's paper environment): candidates when
    `include_candidates`, and, when `include_exploration`, setups that failed validation without losing on unseen data."""
    from ..signals.engine import signal_from_row

    now = rt.clock()
    try:
        adapter = rt.brokers.get(broker_id)
    except Exception:
        raise ValueError(f"Unknown account {broker_id}") from None
    if rt.risk.halted:
        raise ValueError("Trading is halted by the risk engine (kill switch or circuit breaker): no new proposals")
    proposed, sent, skipped = [], [], []
    datasets: dict = {}
    regimes: dict[tuple[str, str], str] = {}
    funnel: dict = {"setups": 0}
    automatic = TradingMode(rt.user_settings.trading_mode) is TradingMode.AUTO
    paper = is_paper_account(adapter)
    # Strongest evidence first: validated setups, then candidates, then exploration, each by out-of-sample return. With
    # one proposal per instrument, this decides which strategy gets it.
    rank = {"validated": 0, "candidate": 1, "exploration": 2}
    company = getattr(rt, "company", None)
    scores: dict[str, float | None] = {}

    def score(iid: str) -> float | None:
        if iid not in scores:
            snap = company.fundamentals_now(iid, now) if company else None
            scores[iid] = snap.score() if snap else None
        return scores[iid]

    # Within a level, companies with stronger reported fundamentals first (instruments without any, after them).
    ordered = sorted(validations(session, now), key=lambda v: (rank.get(level_of(v) or "", 3), -(score(v["instrument_id"]) if score(v["instrument_id"]) is not None else -9),
                                                               -(v["out_of_sample"].get("total_return") or -1e9)))
    for v in ordered:
        iid, tf, strategy = v["instrument_id"], v["timeframe"], v["strategy"]
        if instrument_ids and iid not in instrument_ids:
            continue
        label = {"strategy": strategy, "instrument_id": iid, "timeframe": tf}

        def skip(stage: str, reason: str, label=label, v=v) -> None:
            skipped.append({**label, "level": level_of(v), "recorded": v.get("recorded_status"), "stage": stage, "reason": reason})
            funnel[stage] = funnel.get(stage, 0) + 1

        funnel["setups"] += 1

        level = level_of(v)
        if always_take_profit and level and (v.get("params") or {}).get("take_profit_atr", 0) is None:
            skip("validation", "validated without a take profit, which is now required: waiting for its retest")
            continue
        if level == "candidate":
            if not include_candidates:
                skip("validation", "candidate only (too few unseen trades): not proposed unless candidates are allowed")
                continue
            if not paper:
                skip("validation", "candidate only: weaker evidence is traded on paper accounts, never with real money")
                continue
        elif level == "exploration":
            if not include_exploration:
                skip("validation", "did not pass validation (exploration is off)")
                continue
            if not paper:
                skip("validation", "exploration: traded on paper accounts only, never with real money")
                continue
        elif level != "validated":
            skip("validation", "validation expired; it will be run again" if v["status"] == "expired" else "did not pass validation")
            continue
        try:
            inst = rt.registry.get(iid)
        except KeyError:
            skip("tradable", "instrument no longer in the universe")
            continue
        if not rt.registry.is_mapped(broker_id, iid) or inst.asset_class not in adapter.capabilities.asset_classes:
            skip("tradable", f"not tradable at {broker_id}")
            continue
        try:
            if (iid, tf) not in datasets:  # read once per series, however many strategies look at it
                datasets[(iid, tf)] = bt.load_dataset(session, iid, tf, last=SCAN_BARS)
            data = datasets[(iid, tf)]
        except bt.DataUnavailable as exc:
            skip("data", str(exc))
            continue
        if data.kind is DataKind.SIMULATED:
            skip("data", "only simulated prices are stored")
            continue
        bars = data.bars
        instance = create_strategy(strategy, v["params"])
        if len(bars) < instance.warmup() + 50:
            skip("data", "not enough history for a dependable signal")
            continue
        close_time = bars.index[-1] + tf_delta(tf)
        if not _recent(close_time, tf, now):
            skip("data", f"prices are out of date (last bar closed {close_time:%Y-%m-%d %H:%M} UTC)")
            continue
        if MODULES[v["horizon"]]["needs_live"] and iid not in rt.live.subscribed:
            skip("data", f"{MODULES[v['horizon']]['label']} needs a live feed for this instrument")
            continue
        traded = float((bars["close"] * bars["volume"]).iloc[-20:].mean())
        if traded < min_traded_value:
            skip("liquidity", f"average traded value {traded:,.0f} is below the liquidity floor {min_traded_value:,.0f}")
            continue
        frame = instance.generate(bars)
        if company:
            # The same point-in-time filters the validation applied: an entry it would have refused is not proposed now.
            blocked_any, blocked_long, _ = company.masks(iid, tf, bars.index)
            if blocked_any is not None or blocked_long is not None:
                from .filters import apply_entry_block

                frame = apply_entry_block(frame, blocked_any, blocked_long)
        transform = rt.profile_transform(iid, instance, tf, bars) if hasattr(rt, "profile_transform") else None
        if transform is not None:  # the crypto profile's rules, as in the validation
            frame = transform(frame)
        last = frame.iloc[-1]
        target = int(last["target"])
        if target == 0:
            skip("signal", "the strategy holds no position on the latest bar")
            continue
        # The bar on which the strategy took this position. An entry is still worth proposing for a few bars after
        # it, as long as the price has not run away from it; older than that, the opportunity has passed.
        opened = int((frame["target"] != frame["target"].shift()).to_numpy().nonzero()[0][-1])
        age = len(frame) - 1 - opened
        if age > SCAN["entry_window_bars"]:
            skip("signal", f"in position since {age} bars: the entry is older than the {SCAN['entry_window_bars']}-bar window")
            continue
        stop = float(last["stop_distance"])
        drift = abs(float(last["close"]) - float(frame["close"].iloc[opened]))
        if age and drift > SCAN["max_drift_stop_fraction"] * stop:
            skip("signal", f"the price has moved {drift / stop:.0%} of the stop distance since the entry signal {age} bar(s) ago")
            continue
        # The market regime, only now that there is an entry to judge (it is the costliest part of a scan), once per series.
        if (iid, tf) not in regimes:
            regimes[(iid, tf)] = str(detect_regimes(bars)["primary"].iloc[-1])
        regime = regimes[(iid, tf)]
        fits = MODULES[v["family"]]["fits"]
        if regime == "unknown" and require_regime_fit:
            skip("regime", "market regime cannot be determined yet")
            continue
        # The validation tested the strategy in every regime, so by default the regime is reported, not used as a gate:
        # filtering on it would trade a different rule from the one that was tested.
        if require_regime_fit and regime not in fits:
            skip("regime", f"current regime {regime.replace('_', ' ')} does not suit {MODULES[v['family']]['label'].lower()}")
            continue
        signal = signal_from_row(frame.index[-1], last, instance, iid, tf, close_time).model_copy(
            update={"action": SignalAction.BUY if target > 0 else SignalAction.SELL})
        # Identified by the bar that opened the position, so the same entry is never proposed twice on later bars.
        signal.meta["bar_open"] = frame.index[opened].isoformat()
        signal.meta["signal_age_bars"] = age
        if signal.target < 0 and not (inst.shortable and adapter.capabilities.supports_short):
            skip("signal", "the signal is a short sale, which this account does not allow for the instrument")
            continue
        if company and (why := company.live_block(iid, now, signal.target > 0, tf)):
            skip("events", why)
            continue
        slice_key = (broker_id, iid, strategy)
        if any((a.order.broker_id, a.order.instrument_id, a.order.strategy) == slice_key for a in rt.engine.approvals.values()):
            skip("already", "already proposed and waiting for a decision")
            continue
        if rt.portfolio.qty(slice_key) != 0:
            skip("already", "already in position")
            continue
        # One entry per instrument: several strategies agreeing on the same stock are one bet, not several.
        busy = (any(k[0] == broker_id and k[1] == iid and s.qty for k, s in rt.portfolio.slices.items())
                or any(a.order.broker_id == broker_id and a.order.instrument_id == iid and not a.order.reduce_only for a in rt.engine.approvals.values())
                or any(o.is_open and o.broker_id == broker_id and o.instrument_id == iid and not o.reduce_only for o in rt.engine.orders.values()))
        if busy:
            skip("already", "another strategy already holds or has proposed this instrument")
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
            "opportunity": {"family": v["family"], "horizon": v["horizon"], "regime": regime, "regime_fits": fits, "regime_suits": regime in fits,
                            "average_traded_value": traded,
                            "why": f"{strategy} opened a {'long' if signal.target > 0 else 'short'} position at the close of the bar that started "
                                   f"{frame.index[opened]:%Y-%m-%d %H:%M} UTC" + (f" ({age} bar(s) ago)" if age else "") + f"; the latest close is {signal.price:g}. "
                                   f"Market regime: {regime.replace('_', ' ')}, which "
                                   + ("is one" if regime in fits else "is NOT one") + f" that {MODULES[v['family']]['label'].lower()} is meant for."},
            "validation": {"id": v["id"], "level": level, "tested_at": v["tested_at"], **v["out_of_sample"], "profitable_folds": v["profitable_folds"], "folds": v["folds"],
                           "walk_forward_efficiency": v["walk_forward_efficiency"],
                           "caveat": v["caveat"] if level != "exploration" else (
                               "Exploration: this setup FAILED validation; it is traded on paper only because it did not lose on unseen data, "
                               "to measure it in real time. There is no evidence of an edge.")},
            "data": {"kind": data.kind.value, "source": data.source, "last_bar_close": close_time.isoformat(), "bars": int(len(bars))},
            "fundamentals": (company.fundamentals_now(iid, now).to_dict() if company and company.fundamentals_now(iid, now) else None),
            "filters": {**(v.get("filters") or {}), "news_checked": bool(company and company.settings().news)}})
        before = set(rt.engine.approvals)
        orders_before = set(rt.engine.orders)
        # Recorded before the order is created: the engine refuses weaker evidence on a real-money account, and results
        # are reported per level.
        rt.evidence[rt.evidence_key(broker_id, iid, strategy)] = level
        with rt.lock:
            # Orders are sent without asking only when the user has chosen automatic mode; real-money accounts still wait for approval.
            orders = rt.engine.on_signal(signal, dep, mode_override=None if automatic else TradingMode.SEMI_AUTO)
            rt.persist()
        new = [a for a in rt.engine.approvals if a not in before]
        # Only orders created now count as sent: the same entry signalled again returns the order already sent for it
        # (the engine never sends a client order id twice), which is not a new order.
        created = [o for o in orders if o.id not in orders_before]
        submitted = [o for o in created if o.status.value in ("submitted", "filled", "partially_filled")]
        earlier = [o for o in orders if o.id in orders_before and not o.reduce_only]
        if new:
            proposed.append({**label, "proposal_ids": new, "regime": regime, "level": level})
            funnel["proposed"] = funnel.get("proposed", 0) + 1
        elif submitted:
            sent.append({**label, "order_ids": [o.id for o in submitted], "regime": regime, "level": level})
            funnel["sent"] = funnel.get("sent", 0) + 1
        elif earlier:
            o = earlier[0]
            skip("already", f"this entry was already acted on: order {o.id} {o.status.value}"
                            + (f" ({(o.reason or '').split(' | ')[-1][:120]})" if o.status.value in ("rejected", "cancelled") else "")
                            + "; nothing new was sent")
        else:
            reason = next((o.reason for o in created if o.status.value == "rejected"), None)
            skip("risk" if reason else "already", f"refused by the risk engine: {reason}" if reason else "already proposed for this entry (it was rejected or lapsed)")
    return {"scanned_at": now.isoformat(), "broker_id": broker_id, "proposed": proposed, "sent": sent, "skipped": skipped, "funnel": funnel,
            "validated": sum(1 for v in validations(session, now) if v["status"] == "validated"),
            "candidates": sum(1 for v in validations(session, now) if v["status"] == "candidate"),
            "explorable": sum(1 for v in validations(session, now) if explorable(v))}


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
        except Exception as exc:  # not connected: reported as such below instead of guessing
            out["tradable"][adapter.broker_id] = {"count": 0, "with_prices": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        ids = [i.id for i in instruments if rt.registry.is_mapped(adapter.broker_id, i.id) and i.asset_class in classes]
        out["tradable"][adapter.broker_id] = {"count": len(ids), "with_prices": len(set(ids) & priced), "environment": getattr(adapter, "environment", "paper"),
                                              "meaning": "Instruments this account can actually place orders in."}
    return out
