"""Autopilot: the routine work a person would otherwise do by hand, run on a schedule.

Each cycle: keep the universe and its prices current, pick the liquid instruments, validate the configured
strategies on them (re-validating when a validation has expired), ask for live prices where a strategy is
validated, and scan for opportunities. The result is order PROPOSALS. In semi-automatic mode nothing is
sent without a person's approval, and the autopilot cannot change the trading mode or any risk limit.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

import httpx
import pandas as pd

from ..core.logging import get_logger
from ..domain.models import AssetClass
from ..instruments.registry import market_of
from . import opportunities as opp

log = get_logger(__name__)
ACTOR = "autopilot"


def alpaca_most_active(key: str, secret: str, top: int, base_url: str = "https://data.alpaca.markets", client: httpx.Client | None = None) -> list[str]:
    """Alpaca's official screener: the most traded US stocks of the latest session."""
    r = (client or httpx.Client(timeout=20)).get(f"{base_url.rstrip('/')}/v1beta1/screener/stocks/most-actives", params={"by": "trades", "top": min(top, 100)},
                                                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    r.raise_for_status()
    return [row["symbol"] for row in r.json().get("most_actives", []) if str(row.get("symbol", "")).isalpha()]


class Autopilot:
    def __init__(self, runtime) -> None:
        self.rt = runtime
        self.most_active: Callable[[str, str, int], list[str]] | None = None  # test hook
        self.fetch_fx: Callable[[], list] | None = None  # test hook: [(base, quote, timestamp, rate)]
        self.wait_seconds = 900.0  # how long a cycle waits for a download or a metrics job
        self._lock = threading.Lock()
        self.activity: deque = deque(maxlen=80)
        self.state: dict = {"running": False, "last_cycle": None, "last_result": None, "last_seed": None, "last_price_update": None, "cycles": 0}

    # ---- helpers
    @property
    def cfg(self):
        return self.rt.user_settings.autopilot

    def _note(self, step: str, text: str, level: str = "info") -> None:
        self.activity.appendleft({"ts": self.rt.clock().isoformat(), "step": step, "level": level, "text": text})
        if level != "info":
            log.warning("autopilot", step=step, text=text)

    def _broker(self) -> str | None:
        if self.cfg.broker_id:
            return self.cfg.broker_id
        for adapter in self.rt.brokers.all():
            try:
                if adapter.is_connected() and adapter.capabilities.trading_enabled:
                    return adapter.broker_id
            except Exception:
                continue
        return None

    def _alpaca(self) -> dict | None:
        try:
            found = self.rt._provider_credentials("alpaca")
        except Exception:
            return None
        return found[0] if found and found[0].get("api_key") and found[0].get("api_secret") else None

    def _wait_download(self) -> dict:
        job = self.rt.download
        if job is not None and hasattr(job, "completed"):
            job.completed.wait(self.wait_seconds)
        gm = self.rt.global_markets
        if gm.metrics_job is not None:
            gm.metrics_job.completed.wait(self.wait_seconds)
        return job.snapshot() if job is not None else {}

    def status(self) -> dict:
        now = self.rt.clock()
        last = self.state["last_cycle"]
        due = None
        if self.cfg.enabled:
            due = now.isoformat() if last is None else (datetime.fromisoformat(last) + timedelta(minutes=self.cfg.cycle_minutes)).isoformat()
        return {**self.state, "enabled": self.cfg.enabled, "next_cycle": due, "broker_id": self._broker(), "alpaca_key": self._alpaca() is not None,
                "trading_mode": self.rt.user_settings.trading_mode, "settings": self.cfg.model_dump(), "activity": list(self.activity),
                "sends_orders_by_itself": self.rt.user_settings.trading_mode == "auto",
                "note": "The autopilot prepares data, validates strategies and creates proposals. In semi-automatic mode every order still waits for your approval."}

    # ---- scheduling
    def tick(self) -> dict | None:
        """Called every minute by the application. Runs a cycle when one is due; also keeps proposals and broker orders current."""
        rt = self.rt
        try:
            rt.proposals()  # lapses proposals nobody decided on
            with rt.lock:
                if any(o.is_open and not o.is_simulated_venue for o in rt.engine.orders.values()):
                    rt.engine.poll_orders(rt.clock())
                    rt.persist()
        except Exception as exc:
            self._note("housekeeping", f"{type(exc).__name__}: {exc}", "warning")
        if not self.cfg.enabled:
            return None
        last = self.state["last_cycle"]
        if last is not None and rt.clock() - datetime.fromisoformat(last) < timedelta(minutes=self.cfg.cycle_minutes):
            return None
        return self.run_cycle()

    def run_cycle(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.state["running"] = True
        result: dict = {"started": self.rt.clock().isoformat()}
        try:
            for name, step in (("exchange_rates", self._fx), ("universe", self._seed), ("prices", self._update_prices), ("candidates", self._candidates),
                               ("validation", self._validate), ("live", self._live), ("scan", self._scan)):
                try:
                    result[name] = step(result)
                except Exception as exc:  # one failing step must not stop the others or the next cycle
                    result[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                    self._note(name, result[name]["error"], "warning")
            result.pop("_candidates", None)
            return result
        finally:
            self.state.update(running=False, last_cycle=self.rt.clock().isoformat(), last_result=result, cycles=self.state["cycles"] + 1)
            self._lock.release()

    # ---- steps
    def _us_equities(self) -> list:
        return [i for i in self.rt.registry.all() if i.asset_class in (AssetClass.STOCK, AssetClass.ETF) and market_of(i) == "US"]

    def _priced(self) -> set[str]:
        with self.rt.db.session() as session:
            return {c["instrument_id"] for c in self.rt.store.coverage(session) if c["timeframe"] == "1d" and c["kind"] != "simulated"}

    def _seed(self, _: dict) -> dict:
        cfg, creds, now = self.cfg, self._alpaca(), self.rt.clock()
        if not cfg.seed_most_active:
            return {"skipped": "switched off"}
        if creds is None:
            return {"skipped": "no Alpaca key: the universe is not extended automatically"}
        priced = self._priced()
        have = [i for i in self._us_equities() if i.id in priced]
        last = self.state["last_seed"]
        if len(have) >= cfg.max_instruments or (last and now - datetime.fromisoformat(last) < timedelta(hours=24)):
            return {"skipped": f"{len(have)} US instruments already have prices"}
        fetch = self.most_active or (lambda k, s, n: alpaca_most_active(k, s, n, self.rt.settings.alpaca_base_url))
        symbols = fetch(creds["api_key"], creds["api_secret"], cfg.max_instruments)
        self.state["last_seed"] = now.isoformat()
        added = self.rt.add_instruments(symbols, "US", "stock", ACTOR)
        missing = [i for i in added["created"] + added["existing"] if i not in priced]
        downloaded = 0
        if missing:
            self.rt.start_download("alpaca", ACTOR, instrument_ids=missing, mode="full")
            downloaded = self._wait_download().get("stored", 0)
        self._note("universe", f"{len(added['created'])} of the most traded US stocks added, {len(missing)} histories requested")
        return {"added": added["created"], "histories_requested": len(missing), "bars_stored": downloaded}

    def _fx(self, _: dict) -> dict:
        """The European Central Bank's daily reference rates (public, official), so that positions in other
        currencies can always be valued and sized in the base currency."""
        from ..market_data.fx import ECB_DAILY_URL, parse_ecb_reference_rates

        now, last = self.rt.clock(), self.state.get("last_fx")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=12):
            return {"skipped": "refreshed in the last 12 hours"}
        fetch = self.fetch_fx or (lambda: parse_ecb_reference_rates(httpx.get(ECB_DAILY_URL, timeout=20).text))
        rows = fetch()
        for base, quote, ts, rate in rows:
            self.rt.add_fx_rate(base, quote, rate, "ecb", ACTOR, ts=ts)
        self.state["last_fx"] = now.isoformat()
        return {"rates": len(rows)}

    def _update_prices(self, _: dict) -> dict:
        cfg, now = self.cfg, self.rt.clock()
        if not cfg.daily_price_update:
            return {"skipped": "switched off"}
        if self._alpaca() is None:
            return {"skipped": "no Alpaca key: prices are not refreshed automatically"}
        last = self.state["last_price_update"]
        if last and now - datetime.fromisoformat(last) < timedelta(hours=20):
            return {"skipped": "already updated in the last 20 hours"}
        if not (self._priced() & {i.id for i in self._us_equities()}):
            return {"skipped": "no US instrument has prices yet"}
        self.rt.start_download("alpaca", ACTOR, mode="update")
        snap = self._wait_download()
        self.state["last_price_update"] = now.isoformat()
        failed = snap.get("failed_count", len(snap.get("failed", []) or []))
        self._note("prices", f"daily prices updated: {snap.get('stored', 0)} new bars, {failed} failures")
        return {"new_bars": snap.get("stored", 0), "failed": failed}

    def _candidates(self, result: dict) -> dict:
        """Liquid instruments that the chosen account can trade, most traded first, plus anything with a live feed."""
        from ..global_markets.scanner import universe_frame

        cfg, broker = self.cfg, self._broker()
        if broker is None:
            raise RuntimeError("no trading account is connected: create a paper account under Execution")
        with self.rt.db.session() as session:
            frame = universe_frame(session, self.rt.registry)
        chosen: list[str] = []
        if frame.empty:
            try:
                self.rt.global_markets.start_metrics(ACTOR)
                self.rt.global_markets.metrics_job.completed.wait(self.wait_seconds)
                with self.rt.db.session() as session:
                    frame = universe_frame(session, self.rt.registry)
            except ValueError:
                pass
        if not frame.empty:
            price, traded = pd.to_numeric(frame["close"], errors="coerce"), pd.to_numeric(frame["dollar_volume_20"], errors="coerce")
            ok = frame[(frame["data_kind"] != "simulated") & (price >= cfg.min_price) & (traded >= cfg.min_traded_value)]
            ok = ok.assign(_t=traded).sort_values("_t", ascending=False)
            chosen = [i for i in ok["instrument_id"] if self.rt.registry.is_mapped(broker, i)][: cfg.max_instruments]
        live = [i for i in self.rt.live.subscribed if i not in chosen and self.rt.registry.is_mapped(broker, i)]
        result["_candidates"] = chosen + live
        return {"count": len(chosen) + len(live), "by_liquidity": len(chosen), "live_only": len(live), "broker_id": broker}

    def _validate(self, result: dict) -> dict:
        cfg, now = self.cfg, self.rt.clock()
        candidates = result.get("_candidates", [])
        with self.rt.db.session() as session:
            # A validation, passed or failed, is redone once it is older than its validity: the market it judged has moved on.
            known = {(v["strategy"], v["instrument_id"], v["timeframe"]): ("expired" if v["due_for_retest"] else v["status"])
                     for v in opp.validations(session, now)}
            stored = {(c["instrument_id"], c["timeframe"]) for c in self.rt.store.coverage(session) if c["kind"] != "simulated"}
        todo = [(s, i, tf) for i in candidates for tf in cfg.timeframes for s in cfg.strategies
                if (i, tf) in stored and known.get((s, i, tf)) in (None, "expired")]
        done = passed = failed = 0
        for strategy, iid, tf in todo[: cfg.validations_per_cycle]:
            try:
                with self.rt.db.session() as session:
                    out = opp.validate(session, self.rt, strategy, iid, tf, ACTOR)
                done += 1
                passed += out["status"] == "validated"
            except Exception as exc:  # e.g. too little history for this timeframe
                failed += 1
                self._note("validation", f"{strategy} on {iid} {tf}: {str(exc)[:160]}", "warning")
        if done or failed:
            self._note("validation", f"{done} validations run, {passed} passed, {max(0, len(todo) - done - failed)} still to do")
        return {"run": done, "passed": passed, "could_not_run": failed, "remaining": max(0, len(todo) - done - failed)}

    def _live(self, _: dict) -> dict:
        cfg, now = self.cfg, self.rt.clock()
        if not cfg.live_subscribe:
            return {"skipped": "switched off"}
        with self.rt.db.session() as session:
            wanted = list(dict.fromkeys(v["instrument_id"] for v in opp.validations(session, now) if v["status"] == "validated"))
        new = [i for i in wanted if i not in self.rt.live.subscribed and self.rt.kinds.get(i) != "simulated"][: cfg.max_instruments]
        if not new:
            return {"subscribed": 0}
        out = self.rt.live_subscribe(new, ACTOR)
        if out["subscribed"]:
            self._note("live", f"live prices requested for {', '.join(i.split(':')[1] for i in out['subscribed'])}")
        return {"subscribed": len(out["subscribed"]), "no_live_source": len(out["unavailable"])}

    def _scan(self, _: dict) -> dict:
        broker = self._broker()
        if broker is None:
            raise RuntimeError("no trading account is connected")
        if self.rt.risk.halted:
            return {"skipped": "trading is paused by the risk engine"}
        with self.rt.db.session() as session:
            out = opp.scan(session, self.rt, broker, ACTOR)
        if out["proposed"]:
            names = ", ".join(f"{p['strategy']} on {p['instrument_id'].split(':')[1]}" for p in out["proposed"])
            self._note("scan", f"{len(out['proposed'])} new proposal(s): {names}")
            self.rt.notifier.notify(self.rt.clock(), "approval", "info", f"Autopilot: {len(out['proposed'])} new order proposal(s)", names)
        return {"validated_setups": out["validated"], "proposed": len(out["proposed"]), "not_proposed": len(out["skipped"]),
                "reasons": [f"{s['strategy']} {s['instrument_id'].split(':')[1]} {s['timeframe']}: {s['reason']}" for s in out["skipped"][:40]]}
