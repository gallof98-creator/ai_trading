"""Autopilot: the routine work a person would otherwise do by hand, run on a schedule.

Each cycle: keep the universe and its prices current, pick the liquid instruments, validate the configured
strategies on them (re-validating when a validation has expired), ask for live prices where a strategy is
validated, and scan for opportunities. The result is order PROPOSALS. In semi-automatic mode nothing is
sent without a person's approval, and the autopilot cannot change the trading mode or any risk limit.
"""
from __future__ import annotations

import time

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
from .parallel import ValidationPool, worker_count

log = get_logger(__name__)
ACTOR = "autopilot"
STARTING_UNIVERSE = 30  # the universe starts at this size and grows from there


def alpaca_most_active(key: str, secret: str, top: int, base_url: str = "https://data.alpaca.markets", client: httpx.Client | None = None,
                       by: str = "trades") -> list[str]:
    """Alpaca's official screener: the most traded US stocks of the latest session, by number of trades or by volume.
    It returns at most 100 names per list."""
    r = (client or httpx.Client(timeout=20)).get(f"{base_url.rstrip('/')}/v1beta1/screener/stocks/most-actives", params={"by": by, "top": min(top, 100)},
                                                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    r.raise_for_status()
    return [row["symbol"] for row in r.json().get("most_actives", []) if str(row.get("symbol", "")).isalpha()]


def alpaca_liquid_universe(key: str, secret: str, top: int, base_url: str = "https://data.alpaca.markets") -> list[str]:
    """Both screener lists merged (most trades first, then most volume): up to about 150 different names a day.
    The lists change daily, so a universe larger than that fills up over several days."""
    names = alpaca_most_active(key, secret, 100, base_url, by="trades")
    if top > len(names):
        try:
            names += [s for s in alpaca_most_active(key, secret, 100, base_url, by="volume") if s not in names]
        except httpx.HTTPError:
            pass  # the first list is enough to carry on
    return names


def alpaca_crypto_daily(symbol: str, key: str = "", secret: str = "", years: int = 6, base_url: str = "https://data.alpaca.markets",
                        client: httpx.Client | None = None, now: datetime | None = None) -> pd.DataFrame:
    """Completed daily bars of one crypto pair from Alpaca's official crypto data (works without a key, slowly)."""
    import time

    now = now or datetime.now().astimezone()
    client = client or httpx.Client(timeout=30)
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret} if key else {}
    params: dict = {"symbols": symbol, "timeframe": "1Day", "limit": 10_000, "start": (now - timedelta(days=366 * years)).strftime("%Y-%m-%dT00:00:00Z")}
    rows: list[dict] = []
    for _ in range(20):
        for attempt in range(4):  # without a key only a few requests a minute are allowed
            r = client.get(f"{base_url.rstrip('/')}/v1beta3/crypto/us/bars", params=params, headers=headers)
            if r.status_code in (401, 403) and headers:
                headers = {}  # crypto data needs no key: a refused (e.g. regenerated) key must not stop it
                continue
            if r.status_code != 429:
                break
            time.sleep(3.0 * (attempt + 1))
        r.raise_for_status()
        body = r.json()
        rows += (body.get("bars") or {}).get(symbol) or []
        if not body.get("next_page_token"):
            break
        params["page_token"] = body["next_page_token"]
    frame = pd.DataFrame([[b["o"], b["h"], b["l"], b["c"], b["v"]] for b in rows], columns=["open", "high", "low", "close", "volume"],
                         index=pd.DatetimeIndex([pd.Timestamp(b["t"]) for b in rows], name="ts")).sort_index()
    # The bar of the current day is still forming: a strategy must only ever see completed bars.
    return frame[frame.index < pd.Timestamp(now).tz_convert("UTC").normalize()]


class Autopilot:
    def __init__(self, runtime) -> None:
        self.rt = runtime
        self.most_active: Callable[[str, str, int], list[str]] | None = None  # test hook
        self.fetch_fx: Callable[[], list] | None = None  # test hook: [(base, quote, timestamp, rate)]
        self.fetch_crypto: Callable[[str], pd.DataFrame] | None = None  # test hook: pair -> completed daily bars
        self.wait_seconds = 900.0  # how long a cycle waits for a download or a metrics job
        self.validation_seconds = 3600.0  # how long one cycle may spend validating; what is not finished waits for the next
        self._lock = threading.Lock()
        self.pool = ValidationPool()
        self.activity: deque = deque(maxlen=80)
        self._warned: set[str] = set()
        self._auto_live: set[str] = set()  # instruments the autopilot subscribed (a user's own subscriptions are left alone)
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
        # What the running cycle is doing right now, so that a long first cycle can be told apart from a stuck one.
        current = None
        if self.state.get("running") and self.state.get("step"):
            since = datetime.fromisoformat(self.state["step_started"])
            detail = self.state.get("step_progress")
            job = self.rt.download.snapshot() if self.rt.download is not None else None
            if not detail and self.state["step"] in ("universe", "prices") and job and job.get("status") in ("pending", "running"):
                detail = f"downloading daily prices: {job.get('done', 0)} of {job.get('total', 0)} instruments"
            metrics = self.rt.global_markets.metrics_job
            if not detail and metrics is not None and not metrics.completed.is_set():
                snap = metrics.snapshot()
                detail = f"computing scanner metrics: {snap.get('done', 0)} of {snap.get('total', 0)}"
            current = {"step": self.state["step"], "since": since.isoformat(), "minutes": round((now - since).total_seconds() / 60, 1), "detail": detail}
        return {**self.state, "current": current, "enabled": self.cfg.enabled, "next_cycle": due, "broker_id": self._broker(), "alpaca_key": self._alpaca() is not None,
                "trading_mode": self.rt.user_settings.trading_mode, "settings": self.cfg.model_dump(), "activity": list(self.activity),
                "validation_workers": worker_count(self.cfg.validation_workers),
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
        try:
            self._watch_positions()
        except Exception as exc:
            self._note("housekeeping", f"{type(exc).__name__}: {exc}", "warning")
        if not self.cfg.enabled:
            return None
        last = self.state["last_cycle"]
        if last is not None and rt.clock() - datetime.fromisoformat(last) < timedelta(minutes=self.cfg.cycle_minutes):
            return None
        return self.run_cycle()

    def _watch_positions(self) -> None:
        """A position whose stop is watched by the platform (not resting at the broker) needs live prices. Ask for them,
        and say so loudly when there is no source: until prices arrive, that position has no working stop."""
        rt = self.rt
        need = sorted({key[1] for key in rt.engine.unwatched_positions()} - set(rt.live.subscribed))
        if not need:
            self._warned &= {key[1] for key in rt.engine.unwatched_positions()}
            return
        got = rt.live_subscribe(need, ACTOR)
        for iid in sorted(set(got["unavailable"]) - self._warned):
            self._warned.add(iid)
            rt.notifier.notify(rt.clock(), "risk", "critical", f"No working stop on {iid}",
                               "Its stop is watched by the platform, and no live price source covers this instrument: the stop is only "
                               "checked when new daily prices are downloaded. Close the position or connect a live source.")
            self._note("positions", f"{iid}: stop watched by the platform but no live prices available", "warning")

    def run_cycle(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.state["running"] = True
        result: dict = {"started": self.rt.clock().isoformat()}
        timings: dict[str, float] = {}
        try:
            # Opportunities are looked for BEFORE the long validation work, so a cycle busy re-validating never delays an
            # entry; and once more after it, only if it produced setups that may trade.
            for name, step in (("exchange_rates", self._fx), ("universe", self._seed), ("prices", self._update_prices), ("crypto", self._crypto), ("candidates", self._candidates),
                               ("company", self._company), ("intraday", self._intraday), ("live", self._live), ("scan", self._scan), ("validation", self._validate),
                               ("rescan", self._rescan)):
                self.state.update(step=name, step_started=self.rt.clock().isoformat(), step_progress=None)
                began = time.perf_counter()
                try:
                    out = step(result)
                    if out is not None:
                        result[name] = out
                except Exception as exc:  # one failing step must not stop the others or the next cycle
                    result[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                    self._note(name, result[name]["error"], "warning")
                timings[name] = round(time.perf_counter() - began, 1)
            result.pop("_candidates", None)
            result["seconds_per_step"] = timings
            return result
        finally:
            self.state.update(running=False, step=None, step_started=None, step_progress=None, last_cycle=self.rt.clock().isoformat(),
                              last_result=result, cycles=self.state["cycles"] + 1)
            self._lock.release()

    # ---- steps
    def _us_equities(self) -> list:
        return [i for i in self.rt.registry.all() if i.asset_class in (AssetClass.STOCK, AssetClass.ETF) and market_of(i) == "US"]

    def _priced(self) -> set[str]:
        with self.rt.db.session() as session:
            return {c["instrument_id"] for c in self.rt.store.coverage(session) if c["timeframe"] == "1d" and c["kind"] != "simulated"}

    def _seed(self, _: dict) -> dict:
        cfg, creds, now = self.cfg, self._alpaca(), self.rt.clock()
        if creds is None:
            return {"skipped": "no Alpaca key: the universe is not extended automatically"}
        out: dict = {}
        priced = self._priced()
        # ETFs first: a fixed, user-editable list that is always kept in the universe.
        etfs = [e.strip().upper() for e in cfg.etfs if e.strip()]
        wanted_etfs = self.rt.add_instruments(etfs, "US", "etf", ACTOR) if etfs else {"created": [], "existing": []}
        missing = [i for i in wanted_etfs["created"] + wanted_etfs["existing"] if i not in priced]
        out["etfs"] = len(etfs)
        last = self.state["last_seed"]
        have = [i for i in self._us_equities() if i.id in priced]
        added: list[str] = []
        # The universe grows in steps: `universe_step` instruments a day, and only while the last cycle finished its
        # validations. A machine or a data plan that cannot keep up therefore stops the growth by itself.
        backlog = ((self.state.get("last_result") or {}).get("validation") or {}).get("remaining", 0)
        target = min(cfg.max_instruments, STARTING_UNIVERSE if len(have) < STARTING_UNIVERSE else len(have) + cfg.universe_step)
        out["growth"] = {"instruments": len(have), "target_now": target, "limit": cfg.max_instruments, "paused": bool(backlog and len(have) >= STARTING_UNIVERSE)}
        if backlog and len(have) >= STARTING_UNIVERSE:
            target = len(have)
        if cfg.seed_most_active and len(have) < target and not (last and now - datetime.fromisoformat(last) < timedelta(hours=24)):
            fetch = self.most_active or (lambda k, s, n: alpaca_liquid_universe(k, s, n, self.rt.settings.alpaca_base_url))
            known = {i.symbol.upper() for i in have}
            listed = [s for s in fetch(creds["api_key"], creds["api_secret"], cfg.max_instruments) if s.upper() not in etfs]  # an ETF is never added twice as a stock
            symbols = [s for s in listed if s.upper() not in known][: target - len(have)]
            self.state["last_seed"] = now.isoformat()
            result = self.rt.add_instruments(symbols, "US", "stock", ACTOR)
            added = result["created"]
            missing += [i for i in result["created"] + result["existing"] if i not in priced]
        if not missing:
            out["skipped"] = f"{len(have)} US instruments already have prices"
            return out
        self.rt.start_download("alpaca", ACTOR, instrument_ids=missing, mode="full")
        stored = self._wait_download().get("stored", 0)
        self._note("universe", f"{len(added)} of the most traded US stocks added; {len(missing)} histories requested (ETFs included)")
        out.update(added=added, histories_requested=len(missing), bars_stored=stored)
        return out

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

    def _crypto(self, _: dict) -> dict:
        """Add the configured crypto pairs and keep their daily history current (Alpaca crypto data, no key needed)."""
        from ..market_data.quality import DataKind
        from ..market_data.sources import Dataset

        cfg, now = self.cfg, self.rt.clock()
        pairs = [p.strip().upper() for p in cfg.crypto_pairs if p.strip()]
        if not pairs:
            return {"skipped": "no crypto pairs configured"}
        last = self.state.get("last_crypto")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=20):
            return {"skipped": "already updated in the last 20 hours"}
        added = self.rt.add_instruments(pairs, "", "crypto", ACTOR)
        creds = self._alpaca() or {}
        fetch = self.fetch_crypto or (lambda pair: alpaca_crypto_daily(pair, creds.get("api_key", ""), creds.get("api_secret", ""),
                                                                      base_url=self.rt.settings.alpaca_base_url, now=now))
        stored, failed = 0, []
        for iid in added["created"] + added["existing"]:
            inst = self.rt.registry.get(iid)
            try:
                frame = fetch(inst.symbol)
                if len(frame):
                    stored += self.rt._store_download(inst, Dataset(frame, DataKind.HISTORICAL, "alpaca-crypto")) > 0
                else:
                    failed.append(inst.symbol)
            except Exception as exc:  # a pair Alpaca does not list, a simulated series in the way, a network problem
                failed.append(f"{inst.symbol} ({type(exc).__name__}: {str(exc)[:80]})")
        self.state["last_crypto"] = now.isoformat()
        if stored:
            try:  # the scanner metrics follow the new prices
                self.rt.global_markets.start_metrics(ACTOR)
                self.rt.global_markets.metrics_job.completed.wait(self.wait_seconds)
            except ValueError:
                pass
        self._note("crypto", f"{stored} crypto histories refreshed" + (f"; not available: {', '.join(failed)}" if failed else ""), "warning" if failed else "info")
        return {"pairs": len(pairs), "refreshed": stored, "not_available": failed}

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
        # Crypto is not ranked by traded value: the volume in Alpaca's crypto bars is Alpaca's own venue, a small part of the market.
        priced = self._priced()
        wanted = {f"CRYPTO:{p.strip().upper()}" for p in cfg.crypto_pairs}
        crypto = [i for i in sorted(wanted) if i in priced and i not in chosen and self.rt.registry.is_mapped(broker, i)]
        live = [i for i in self.rt.live.subscribed if i not in chosen and i not in crypto and self.rt.registry.is_mapped(broker, i)]
        result["_candidates"] = chosen + crypto + live
        return {"count": len(chosen) + len(crypto) + len(live), "by_liquidity": len(chosen), "crypto": len(crypto), "live_only": len(live), "broker_id": broker}

    def _intraday(self, result: dict) -> dict:
        """Months of official intraday bars for the candidates, when an intraday timeframe is configured. The download
        runs in the background over several cycles; a series is validated only once it is complete."""
        from ..market_data.intraday import INTRADAY

        cfg, data = self.cfg, self.rt.intraday
        timeframes = [tf for tf in cfg.timeframes if tf in INTRADAY]
        if not timeframes or cfg.intraday_history_months <= 0:
            return {"skipped": "no intraday timeframe is configured" if not timeframes else "automatic history download is switched off"}
        job = data.job
        if job is not None and job["status"] == "running":
            return {"downloading": f"{job['done']} of {job['series']} series", "bars": job["bars"]}
        candidates = result.get("_candidates", [])
        history, now = data._history(), self.rt.clock()
        # A series is requested again after a day, so that it is extended when the configured months are increased.
        todo = [i for i in candidates if any(
            (rec := history.get(f"{i}|{tf}")) is None or rec.get("months", 0) < cfg.intraday_history_months for tf in timeframes)]
        if not todo:
            return {"skipped": f"history present for all {len(candidates)} candidates"}
        try:
            data.backfill(todo, timeframes, cfg.intraday_history_months, ACTOR, feed=cfg.intraday_feed)
        except ValueError as exc:
            return {"skipped": str(exc)}
        self._note("intraday", f"intraday history requested for {len(todo)} instruments ({', '.join(timeframes)}, {cfg.intraday_history_months} months, "
                               f"{cfg.intraday_feed.upper()} feed)")
        return {"requested": len(todo), "timeframes": timeframes, "months": cfg.intraday_history_months, "feed": cfg.intraday_feed, "started": now.isoformat()}

    def _validate(self, result: dict) -> dict:
        from ..market_data.timeframes import tf_delta

        cfg, now = self.cfg, self.rt.clock()
        candidates = result.get("_candidates", [])
        downloading = self.rt.intraday.incomplete()
        with self.rt.db.session() as session:
            # A validation, passed or failed, is redone once it is older than its validity: the market it judged has moved on.
            # ... and when the entry filters it was tested with changed (results dates or reported figures became available,
            # or a filter was switched on or off): what is traded must be what was tested.
            none = {"earnings": False, "fundamentals": False}
            known = {(v["strategy"], v["instrument_id"], v["timeframe"]):
                     ("expired" if v["due_for_retest"] or (v.get("filters") or none) != self.rt.company.signature(v["instrument_id"], v["timeframe"]) else v["status"])
                     for v in opp.validations(session, now)}
            stored = {(c["instrument_id"], c["timeframe"]) for c in self.rt.store.coverage(session) if c["kind"] != "simulated"}
        todo = [(s, i, tf) for i in candidates for tf in cfg.timeframes for s in cfg.strategies
                if (i, tf) in stored and (i, tf) not in downloading and known.get((s, i, tf)) in (None, "expired")]
        # Breadth first: every candidate gets a first test before any gets a second one, and daily data (complete, quick to
        # test) goes before minute data. Depth first, a large universe would spend days on its first few instruments.
        rank = {iid: k for k, iid in enumerate(candidates)}
        order = {name: k for k, name in enumerate(cfg.strategies)}
        todo.sort(key=lambda t: (-tf_delta(t[2]), order.get(t[0], 0), rank.get(t[1], 0)))
        waiting = [k for k in {(i, tf) for i in candidates for tf in cfg.timeframes} if k in downloading or k not in stored]
        if not todo and waiting:
            self._note("validation", f"nothing to validate yet: {len(waiting)} candidate series still have no complete history "
                                     f"({', '.join(sorted({tf for _, tf in waiting}))}). Add the 1d timeframe to validate on daily data meanwhile.", "warning")
        batch = todo[: cfg.validations_per_cycle]
        done = passed = failed = promising = 0
        # Read each series once, however many strategies are tested on it; then compute side by side; then record.
        specs, labels, datasets = [], [], {}
        with self.rt.db.session() as session:
            for strategy, iid, tf in batch:
                try:
                    bars = cfg.validation_bars if tf_delta(tf) >= pd.Timedelta(days=1) else cfg.intraday_validation_bars
                    if (iid, tf) not in datasets:
                        datasets[(iid, tf)] = opp.bt.load_dataset(session, iid, tf, last=bars)
                    specs.append(opp.prepare(session, self.rt, strategy, iid, tf, allow_short=cfg.allow_short, max_bars=bars, data=datasets[(iid, tf)]))
                    labels.append((strategy, iid, tf))
                except Exception as exc:  # e.g. an unknown strategy name
                    failed += 1
                    self._note("validation", f"{strategy} on {iid} {tf}: {str(exc)[:160]}", "warning")
        datasets.clear()
        unfinished = 0
        for (strategy, iid, tf), spec, outcome in zip(labels, specs, self.pool.run(
                specs, cfg.validation_workers, timeout=self.validation_budget(),
                progress=lambda done, total: self.state.update(step_progress=f"{done} of {total} validations done"))):
            if outcome is None:
                unfinished += 1  # out of time: it stays on the list for the next cycle
            elif isinstance(outcome, Exception):  # e.g. too little history for this timeframe
                failed += 1
                self._note("validation", f"{strategy} on {iid} {tf}: {str(outcome)[:160]}", "warning")
            else:
                tier, payload = opp.judge(spec, outcome, ACTOR, now)
                with self.rt.db.session() as session:
                    opp.record(session, tier, payload, now)
                done += 1
                passed += tier == "validated"
                promising += tier == "candidate"
        remaining = max(0, len(todo) - done - failed)
        if done or failed:
            speed = self.pool.last
            self._note("validation", f"{done} validations run: {passed} validated, {promising} candidates, {done - passed - promising} rejected; "
                                     f"{remaining} still to do" + (f" ({speed['seconds']}s on {speed['workers']} core(s))" if speed else ""))
        out = {"run": done, "passed": passed, "candidates": promising, "could_not_run": failed, "remaining": remaining}
        if specs:
            out["speed"] = dict(self.pool.last)
            with self.rt.db.session() as session:
                out["multiple_testing"] = opp.multiple_testing(session, now)
        return out

    def _company(self, result: dict) -> dict:
        """Results dates and reported figures from the SEC for the followed US stocks, and recent news from Alpaca for
        every followed instrument: what the entry filters read (see quant_ai/filters.py)."""
        rt, now = self.rt, self.rt.clock()
        held = [k[1] for k, s in rt.portfolio.slices.items() if s.qty]
        ids = list(dict.fromkeys([*held, *result.get("_candidates", []), *rt.live.subscribed]))
        sec, news = rt.company.refresh_sec(ids, now), rt.company.refresh_news(ids, now)
        for name, part in (("SEC", sec), ("news", news)):
            if part.get("error"):
                self._note("company", f"{name}: {part['error']}", "warning")
        status = rt.company.status()
        # Flat, so the panel can show it on one line.
        return {"stocks_with_results_dates": status["with_earnings_dates"], "stocks_with_fundamentals": status["with_fundamentals"],
                "sec_refreshed_now": sec.get("filings_refreshed", 0), "sec": sec.get("error") or sec.get("skipped") or "ok",
                "new_news_items": news.get("new_items", 0), "news": news.get("error") or news.get("skipped") or "ok", "detail": {"sec": sec, "news": news}}

    def _rescan(self, result: dict) -> dict | None:
        v = result.get("validation") or {}
        if not (v.get("passed") or v.get("candidates")):
            return None
        out = self._scan(result)
        before = result.get("scan") or {}
        # One account of the cycle's scans: the latest view of every setup, with the entries of both passes counted.
        for k in ("proposed", "sent"):
            if isinstance(out.get(k), int) and isinstance(before.get(k), int):
                out[k] += before[k]
        out["passes"] = 2
        result["scan"] = out
        return None

    def validation_budget(self) -> float:
        """Seconds one cycle may spend validating: most of the cycle, never an hour of it. Unfinished work waits."""
        return min(self.validation_seconds, max(120.0, self.cfg.cycle_minutes * 60 * 0.6))

    def _live(self, result: dict) -> dict:
        """Live prices for every followed instrument, in order of importance: open positions and working orders, then
        instruments with a validated (or candidate) strategy, then the other candidates, most traded first. Stocks beyond
        the plan's streaming limit are polled; instruments that dropped out of the list are released (never a held one)."""
        cfg, now, rt = self.cfg, self.rt.clock(), self.rt
        if not cfg.live_subscribe:
            return {"skipped": "switched off"}
        held = ([k[1] for k, s in rt.portfolio.slices.items() if s.qty] + [o.instrument_id for o in rt.engine.orders.values() if o.is_open]
                + [a.order.instrument_id for a in rt.engine.approvals.values()])
        with rt.db.session() as session:
            levels = ("validated", "candidate") if cfg.paper_candidates else ("validated",)
            setups = [v["instrument_id"] for v in opp.validations(session, now) if v["status"] in levels]
        others = result.get("_candidates", []) if cfg.live_all_candidates else []
        ordered = list(dict.fromkeys(held + setups + others))
        ordered = ordered[: max(cfg.max_instruments, len(set(held)))]
        released = [i for i in self._auto_live if i not in ordered and i not in held]
        if released:
            rt.live_unsubscribe(released, ACTOR)
        out = rt.live_follow(ordered, ACTOR)
        self._auto_live = set(ordered)
        if out["newly"]:
            self._note("live", f"live prices for {out['followed']} instruments: {out['streamed']} streamed, {out['polled']} by REST requests "
                               f"(Alpaca streams at most {cfg.stream_symbol_limit} stock symbols on this plan)")
        return {**out, "released": len(released)}

    def _scan(self, _: dict) -> dict:
        broker = self._broker()
        if broker is None:
            raise RuntimeError("no trading account is connected")
        if self.rt.risk.halted:
            return {"skipped": "trading is paused by the risk engine"}
        with self.rt.db.session() as session:
            out = opp.scan(session, self.rt, broker, ACTOR, include_candidates=self.cfg.paper_candidates, require_regime_fit=self.cfg.require_regime_fit,
                           include_exploration=self.cfg.exploration)
        if out["proposed"]:
            names = ", ".join(f"{p['strategy']} on {p['instrument_id'].split(':')[1]}" for p in out["proposed"])
            self._note("scan", f"{len(out['proposed'])} new proposal(s): {names}")
            self.rt.notifier.notify(self.rt.clock(), "approval", "info", f"Autopilot: {len(out['proposed'])} new order proposal(s)", names)
        if out["sent"]:
            names = ", ".join(f"{p['strategy']} on {p['instrument_id'].split(':')[1]}" for p in out["sent"])
            self._note("scan", f"{len(out['sent'])} order(s) sent automatically: {names}")
            self.rt.notifier.notify(self.rt.clock(), "execution", "info", f"Autopilot: {len(out['sent'])} order(s) sent", names)
        # Where the setups stopped, most common reason first: this is what to look at when nothing is proposed.
        blockers: dict[str, int] = {}
        for s in out["skipped"]:
            key = s["reason"].split(" (")[0].split(":")[0]
            blockers[key] = blockers.get(key, 0) + 1
        return {"validated_setups": out["validated"], "candidate_setups": out["candidates"], "explorable_setups": out["explorable"], "proposed": len(out["proposed"]), "sent": len(out["sent"]), "not_proposed": len(out["skipped"]),
                "funnel": out["funnel"], "blockers": sorted(([k, n] for k, n in blockers.items()), key=lambda x: -x[1])[:8],
                # Tradable levels first (validated, candidate, exploration), each marked, so the setups that could have traded are on top.
                "reasons": [(f"[{s['level']}] " if s.get("level") else "") + f"{s['strategy']} {s['instrument_id'].split(':')[1]} {s['timeframe']}: {s['reason']}"
                            for s in sorted(out["skipped"], key=lambda s: {"validated": 0, "candidate": 1, "exploration": 2}.get(s.get("level") or "", 3))[:60]]}
