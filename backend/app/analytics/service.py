"""Trade measurement in the running platform: shadow exits of manual closes (live and backfilled) and the statistics."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import select

from ..core.logging import get_logger
from ..database.models import StrategyDeployment
from ..market_data.timeframes import tf_delta
from ..strategies import indicators as ind
from ..strategies.registry import create_strategy
from . import report
from .shadow import shadow_r, simulate_exit

log = get_logger(__name__)
PROTECTIVE = ("stop_loss", "trailing_stop", "take_profit_or_stop")


class TradeAnalytics:
    def __init__(self, rt) -> None:
        self.rt = rt

    @property
    def cfg(self):
        return self.rt.user_settings.analytics

    # ------------------------------------------------------------------ live: a trade has just closed
    def on_trade_closed(self, trade, prot) -> None:
        """Called by the trading engine for every reduced or closed position. A manual exit starts its shadow."""
        if trade.exit_reason not in self.cfg.shadow_exit_reasons:
            return
        dep = self._deployment(trade.broker_id, trade.instrument_id, trade.strategy)
        target = (trade.entry_price + trade.direction * prot.take_profit_distance) if prot and prot.take_profit_distance else None
        trade.shadow = {"status": "pending", "source": "live", "stop": prot.stop_price if prot else None, "target": target,
                        "trailing": prot.trailing_distance if prot else None, "from": trade.exit_ts.isoformat(),
                        "timeframe": dep["timeframe"] if dep else trade.timeframe, "params": dep["params"] if dep else None}
        if not trade.shadow["stop"] and not target and not dep:
            trade.shadow["status"] = "not_reconstructible"
            trade.shadow["why"] = "no stop, no target and no strategy known for this position"

    def _deployment(self, broker_id: str, instrument_id: str, strategy: str | None) -> dict | None:
        for d in self.rt.deployments.values():
            if d.broker_id == broker_id and d.instrument_id == instrument_id and d.strategy.name == strategy:
                return {"timeframe": d.timeframe, "params": d.strategy.params.model_dump()}
        with self.rt.db.session() as session:
            row = session.scalar(select(StrategyDeployment).where(StrategyDeployment.strategy == (strategy or ""),
                                                                  StrategyDeployment.instrument_id == instrument_id,
                                                                  StrategyDeployment.broker_account_id == broker_id))
            return {"timeframe": row.timeframe, "params": dict(row.params or {})} if row else None

    # ------------------------------------------------------------------ backfill of trades recorded before measurement
    def backfill(self) -> dict:
        """Fill in what older trades lack: the base-currency P&L (it was always recorded, as `pnl`), the initial risk and R
        where the stop can be reconstructed, and a shadow for manual exits. Nothing is invented: what cannot be
        reconstructed is marked so."""
        done = {"pnl_base": 0, "risk": 0, "shadow_seeded": 0, "not_reconstructible": 0}
        for t in self.rt.portfolio.trades:
            if t.pnl_net_quote is None:
                t.pnl_net_quote = t.pnl_quote - (t.funding_quote or 0.0)
            if t.pnl_base is None and t.fx_source is None:
                t.pnl_base, t.fx_source = t.pnl, "recorded at the close (rate not stored)"
                done["pnl_base"] += 1
            if t.asset_class is None:
                try:
                    t.asset_class = self.rt.registry.get(t.instrument_id).asset_class.value
                except KeyError:
                    t.asset_class = t.instrument_id.split(":")[0].lower()
            levels = None
            if t.initial_stop_distance is None or (t.exit_reason in self.cfg.shadow_exit_reasons and t.shadow is None):
                levels = self._reconstruct_levels(t)
            if t.initial_stop_distance is None and levels and levels.get("initial_stop_distance"):
                self._set_risk(t, levels["initial_stop_distance"], levels["source"])
                done["risk"] += 1
            if t.exit_reason in self.cfg.shadow_exit_reasons and t.shadow is None:
                if levels and (levels.get("stop") or levels.get("target") or levels.get("params") is not None):
                    t.shadow = {"status": "pending", "source": f"backfill ({levels['source']})", "stop": levels.get("stop"),
                                "target": levels.get("target"), "trailing": None, "from": t.exit_ts.isoformat(),
                                "timeframe": levels.get("timeframe") or t.timeframe, "params": levels.get("params")}
                    done["shadow_seeded"] += 1
                else:
                    t.shadow = {"status": "not_reconstructible", "why": "no recorded stop or target and no strategy settings for it"}
                    done["not_reconstructible"] += 1
        return done

    def _set_risk(self, t, distance: float, source: str) -> None:
        try:
            mult = self.rt.registry.get(t.instrument_id).contract_multiplier
        except KeyError:
            mult = 1.0
        t.initial_stop_distance = distance
        t.initial_risk_quote = distance * t.qty * mult
        factor = (t.pnl_base / t.pnl_net_quote) if t.pnl_base is not None and t.pnl_net_quote else (t.fx_rate or None)
        t.initial_risk_base = t.initial_risk_quote * factor if factor else None
        t.r_multiple = t.pnl_net_quote / t.initial_risk_quote if t.initial_risk_quote else None
        t.risk_source = source

    def _reconstruct_levels(self, t) -> dict | None:
        """Stop and target of a past trade: from the protective orders recorded for it, else from the strategy's settings
        and the ATR at the entry bar (as the strategy computed them)."""
        key = (t.broker_id, t.instrument_id, t.strategy)
        orders = [o for o in self.rt.engine.orders.values() if (o.broker_id, o.instrument_id, o.strategy) == key
                  and (o.reason or "").split(" | ")[0] in PROTECTIVE and t.entry_ts <= o.created_at <= t.exit_ts]
        dep = self._deployment(t.broker_id, t.instrument_id, t.strategy)
        out: dict = {"timeframe": dep["timeframe"] if dep else t.timeframe, "params": dep["params"] if dep else None}
        if orders:
            first, last = min(orders, key=lambda o: o.created_at), max(orders, key=lambda o: o.created_at)
            out.update(stop=last.stop_price, target=last.limit_price if last.oco or (last.reason or "").startswith("take_profit") else None,
                       initial_stop_distance=abs(t.entry_price - first.stop_price) if first.stop_price else None, source="recorded orders")
            return out
        if dep and dep["params"] is not None and out["timeframe"]:
            with self.rt.db.session() as session:
                data = self.rt.store.load(session, t.instrument_id, out["timeframe"])
            if data is None or data.bars.empty:
                return out if dep else None
            bars = data.bars
            try:
                strategy = create_strategy(t.strategy, dep["params"])
            except Exception:  # the strategy's settings are no longer valid
                return None
            p = strategy.params
            atr = ind.atr(bars, p.atr_period)
            before = atr[atr.index + tf_delta(out["timeframe"]) <= pd.Timestamp(t.entry_ts)]
            if before.empty or pd.isna(before.iloc[-1]):
                return out
            a = float(before.iloc[-1])
            stop_d = a * p.stop_atr
            out.update(stop=t.entry_price - t.direction * stop_d, initial_stop_distance=stop_d,
                       target=t.entry_price + t.direction * a * p.take_profit_atr if p.take_profit_atr else None,
                       source="strategy settings and ATR at entry")
            return out
        return None

    # ------------------------------------------------------------------ following the virtual positions
    def update_shadows(self) -> dict:
        now = pd.Timestamp(self.rt.clock())
        counts = {"pending": 0, "finished": 0, "no_data": 0}
        cache: dict = {}
        for t in self.rt.portfolio.trades:
            sh = t.shadow
            if not sh or sh.get("status") != "pending":
                continue
            tf = sh.get("timeframe")
            if not tf:
                sh["status"], sh["why"] = "not_reconstructible", "timeframe unknown"
                continue
            if (t.instrument_id, tf) not in cache:
                with self.rt.db.session() as session:
                    data = self.rt.store.load(session, t.instrument_id, tf)
                cache[(t.instrument_id, tf)] = data.bars if data is not None else None
            bars = cache[(t.instrument_id, tf)]
            if bars is None or bars.empty:
                counts["no_data"] += 1
                continue
            target_series = None
            if sh.get("params") is not None:
                try:
                    target_series = create_strategy(t.strategy, sh["params"]).generate(bars)["target"]
                except Exception as exc:  # the strategy cannot be rebuilt: price levels only
                    sh["signal_note"] = f"strategy exit not evaluated ({type(exc).__name__})"
            result = simulate_exit(bars, pd.Timestamp(sh["from"]), t.direction, sh.get("stop"), sh.get("target"), sh.get("trailing"),
                                   target_series, tf_delta(tf), self.cfg.shadow_max_days, self.cfg.shadow_same_bar_rule, now)
            if result.reason is None:
                counts["pending"] += 1
                continue
            price = result.price
            if price is None:
                sh.update(status="not_reconstructible", why="no market data after the manual exit")
                continue
            try:
                mult = self.rt.registry.get(t.instrument_id).contract_multiplier
            except KeyError:
                mult = 1.0
            costs = (t.commission or 0.0) + (t.funding_quote or 0.0)
            pnl_quote = (price - t.entry_price) * t.direction * t.qty * mult - costs
            sh.update(status="done", exit_reason=result.reason, exit_price=price, exit_ts=result.ts.isoformat() if result.ts is not None else None,
                      bars=result.bars, pnl_quote=pnl_quote, pnl_r=shadow_r(t.direction, t.entry_price, price, t.qty, mult, t.initial_risk_quote, costs))
            counts["finished"] += 1
        return counts

    # ------------------------------------------------------------------ statistics
    def report(self, by: tuple[str, ...] = ("asset_class", "strategy", "exit_reason"), since: datetime | None = None) -> dict:
        trades = [t.to_dict() for t in self.rt.portfolio.trades if since is None or t.exit_ts >= since]
        for d in trades:  # older trades: base P&L was always recorded as `pnl`
            if d.get("pnl_base") is None and d.get("fx_source") is None:
                d["pnl_base"] = d["pnl"]
            d["asset_class"] = d.get("asset_class") or d["instrument_id"].split(":")[0].lower()
        out = report.breakdown(trades, self.cfg.min_sample, by)
        out["base_currency"] = self.rt.portfolio.base
        out["notes"] = ["P&L in the base currency at the exchange rate of each trade's close, after commissions and funding; spread and "
                        "slippage are inside the fill prices (their estimate is shown apart, not deducted twice).",
                        "R = net P&L / initial risk (stop distance at entry x quantity). Trades without a known initial stop have no R.",
                        f"Below {self.cfg.min_sample} trades a group shows 'insufficient sample' instead of a win rate."]
        return out

