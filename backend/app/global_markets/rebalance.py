"""Turning a cross-sectional strategy into paper orders.

Nothing here decides risk: every order is judged by the central RiskEngine, when the proposal is
prepared and again when it is executed. Global strategies trade on paper accounts only.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import ResearchItem
from ..domain.models import Order, Side, TradingMode
from ..market_data.fx import FxRateUnavailable
from ..monitoring import audit
from ..paper_trading.broker import SimulatedBroker
from ..risk.models import Exposure, OrderIntent
from .cross_section import STRATEGY_PREFIX, StrategySpec, load_panel, target_portfolio

PROPOSAL_MAX_AGE = timedelta(hours=24)


class RebalanceError(ValueError):
    pass


def _paper(rt, broker_id: str) -> SimulatedBroker:
    try:
        adapter = rt.brokers.get(broker_id)
    except Exception:
        raise RebalanceError(f"Unknown account {broker_id}") from None
    if not isinstance(adapter, SimulatedBroker):
        raise RebalanceError("Global strategies trade on paper accounts only. Live execution is not part of this module.")
    return adapter


def holdings(rt, broker_id: str | None = None) -> list[dict]:
    ts = rt.clock()
    out = []
    for (b, iid, strategy), s in rt.portfolio.slices.items():
        if not (strategy or "").startswith(STRATEGY_PREFIX) or not s.qty or (broker_id and b != broker_id):
            continue
        inst = rt.registry.get(iid)
        price = rt.portfolio.prices.get(iid, s.avg_price)
        prot = rt.engine.protections.get((b, iid, strategy))
        out.append({"broker_id": b, "instrument_id": iid, "symbol": inst.symbol, "strategy": strategy, "qty": s.qty, "avg_price": s.avg_price, "price": price,
                    "currency": inst.quote_currency, "unrealized_pct": price / s.avg_price - 1 if s.avg_price else None,
                    "stop_price": prot.stop_price if prot else None, "entered": s.entry_ts.isoformat(), "last_bar": rt.driven.get(iid)})
    pending = [{"broker_id": o.broker_id, "instrument_id": o.instrument_id, "side": o.side.value, "qty": o.qty, "type": o.type.value, "reason": o.reason,
                "created_at": o.created_at.isoformat()} for o in rt.engine.orders.values()
               if o.is_open and (o.strategy or "").startswith(STRATEGY_PREFIX) and (not broker_id or o.broker_id == broker_id)]
    return {"as_of": ts.isoformat(), "positions": out, "open_orders": pending}


def propose(rt, session: Session, spec: StrategySpec, broker_id: str, actor: str) -> dict:
    with rt.lock:
        _paper(rt, broker_id)
        target = target_portfolio(load_panel(session, rt.registry, spec.markets or None), spec)
        if not target["enough_history"]:
            raise RebalanceError("Not enough stored history for this model yet (about thirteen months of daily prices are needed).")
        name, ts = spec.strategy_name, rt.clock()
        state = rt.portfolio.state(ts)
        equity, limits = state.equity, rt.user_settings.risk
        held = {k[1]: s.qty for k, s in rt.portfolio.slices.items() if k[0] == broker_id and k[2] == name and s.qty}
        busy = {o.instrument_id for o in rt.engine.orders.values() if o.is_open and o.broker_id == broker_id and o.strategy == name and o.reason == "rebalance"}
        wanted = {r["instrument_id"]: r for r in target["rows"]}
        orders = []
        for iid, qty in sorted(held.items()):
            if iid in wanted or iid in busy:
                continue
            inst = rt.registry.get(iid)
            orders.append({"instrument_id": iid, "symbol": inst.symbol, "side": "SELL", "qty": abs(qty), "price": rt.portfolio.prices.get(iid), "weight": 0.0,
                           "approved": True, "reasons": ["leaves the selection; closing orders are always allowed"], "stop_price": None})
        for iid, row in wanted.items():
            if iid in held or iid in busy:
                continue
            inst, price = rt.registry.get(iid), row["price"]
            entry = {"instrument_id": iid, "symbol": inst.symbol, "side": "BUY", "qty": 0.0, "price": price, "weight": row["weight"], "approved": False,
                     "reasons": [], "stop_price": inst.round_price(price * (1 - spec.stop_pct))}
            orders.append(entry)
            try:
                fx = rt.engine._fx(inst.quote_currency, ts)
            except FxRateUnavailable as exc:
                entry["reasons"].append(f"no exchange rate to value it: {exc}")
                continue
            wanted_value = row["weight"] * equity
            cost = rt.cost_per_unit(inst, price)
            # Loss at the stop including estimated costs must fit the risk-per-trade budget.
            caps = {"target weight": wanted_value, "risk per trade at the stop": limits.risk_per_trade * equity * price / (price * spec.stop_pct + cost),
                    "exposure limits": rt.risk.headroom(broker_id, inst, name, state)}
            binding = min(caps, key=caps.get)
            qty = inst.round_qty(caps[binding] / (price * inst.contract_multiplier * fx))
            if qty <= 0 or qty < inst.min_qty:
                entry["reasons"].append(f"the size allowed by {binding} is below one tradable unit")
                continue
            order = Order(broker_id=broker_id, instrument_id=iid, side=Side.BUY, qty=qty, strategy=name, reason="rebalance", created_at=ts, updated_at=ts)
            intent = OrderIntent(order, inst, price, fx, price * spec.stop_pct, cost_per_unit=cost)
            decision = rt.risk.evaluate(intent, state)
            entry.update(qty=qty, approved=decision.approved, value_base=intent.signed_notional, actual_weight=intent.signed_notional / equity if equity > 0 else None,
                         reasons=list(decision.reasons) + ([f"size reduced by {binding}"] if binding != "target weight" else []))
            if decision.approved:  # later orders are judged against a book that already contains this one
                state.exposures.append(Exposure(broker_id, iid, name, inst.asset_class.value, inst.quote_currency, intent.signed_notional,
                                                abs(intent.signed_notional) * inst.margin_rate))
        payload = {"definition": spec.model_dump(), "strategy": name, "broker_id": broker_id, "as_of": target["as_of"], "created_at": ts.isoformat(),
                   "equity": equity, "base_currency": rt.portfolio.base, "eligible": target["eligible"], "universe": target["universe"], "note": target["note"],
                   "orders": orders, "kept": sorted(set(held) & set(wanted)), "results": None}
        row = ResearchItem(id=uuid.uuid4().hex, kind="rebalance", status="proposed", title=name, created_at=ts, payload=payload)
        session.add(row)
        audit.record(session, actor, "global.rebalance_proposed", proposal=row.id, strategy=name, broker=broker_id, orders=len(orders))
        return {"id": row.id, "status": row.status, **payload}


def execute(rt, session: Session, proposal_id: str, actor: str) -> dict:
    with rt.lock:
        row = session.get(ResearchItem, proposal_id)
        if row is None or row.kind != "rebalance":
            raise RebalanceError("No such rebalance proposal")
        if row.status != "proposed":
            raise RebalanceError(f"This proposal was already {row.status}")
        p, ts = dict(row.payload), rt.clock()
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=ts.tzinfo)
        if ts - created > PROPOSAL_MAX_AGE:
            row.status = "expired"
            raise RebalanceError("This proposal is more than 24 hours old. Prepare a new one from current prices.")
        _paper(rt, p["broker_id"])
        spec = StrategySpec.model_validate(p["definition"])
        results = []
        # Executing a proposal is an explicit human decision, so it is allowed in every trading mode.
        # Sells first; every order goes through the same routing as strategy orders: risk engine, then the broker.
        for o in sorted(p["orders"], key=lambda o: o["side"] != "SELL"):
            if not o["approved"] or o["qty"] <= 0:
                results.append({"instrument_id": o["instrument_id"], "side": o["side"], "status": "not sent", "reason": "; ".join(o["reasons"])})
                continue
            inst = rt.registry.get(o["instrument_id"])
            buy = o["side"] == "BUY"
            price = o["price"] or rt.portfolio.prices.get(inst.id)
            try:
                fx = rt.engine._fx(inst.quote_currency, ts)
            except FxRateUnavailable as exc:
                results.append({"instrument_id": inst.id, "side": o["side"], "status": "not sent", "reason": str(exc)})
                continue
            order = Order(id=rt.engine._new_id(), client_order_id=f"rebal:{row.id}:{inst.id}", broker_id=p["broker_id"], instrument_id=inst.id,
                          side=Side.BUY if buy else Side.SELL, qty=o["qty"], strategy=p["strategy"], reduce_only=not buy, reason="rebalance",
                          created_at=ts, updated_at=ts)
            intent = OrderIntent(order, inst, price, fx, price * spec.stop_pct if buy else None, cost_per_unit=rt.cost_per_unit(inst, price) if buy else 0.0)
            sent = rt.engine._route(order, intent, (price * spec.stop_pct, None, None) if buy else None, TradingMode.AUTO, ts)
            results.append({"instrument_id": inst.id, "side": o["side"], "qty": o["qty"], "status": sent.status.value, "reason": sent.reason, "order_id": sent.id})
        p["results"], p["executed_at"] = results, ts.isoformat()
        row.payload, row.status = p, "executed"
        audit.record(session, actor, "global.rebalance_executed", proposal=row.id, strategy=p["strategy"], broker=p["broker_id"],
                     sent=sum(r["status"] not in ("not sent", "rejected") for r in results))
        rt.persist()
        return {"id": row.id, "status": row.status, **p,
                "next": "Orders fill at the open of the next daily bar you download (Update prices). Each purchase then gets its protective stop."}


def proposals(session: Session, limit: int = 20) -> list[dict]:
    rows = session.scalars(select(ResearchItem).where(ResearchItem.kind == "rebalance").order_by(ResearchItem.created_at.desc()).limit(limit))
    return [{"id": r.id, "status": r.status, "strategy": r.title, "created_at": r.created_at.isoformat(), "broker_id": r.payload["broker_id"],
             "as_of": r.payload["as_of"], "orders": len(r.payload["orders"]), "approved": sum(o["approved"] for o in r.payload["orders"]),
             "results": r.payload.get("results")} for r in rows]
