"""Intraday bars from Alpaca's official history: a refresh every minute, and the download of months of history.

Before this module the one-minute bars of a followed instrument were rebuilt on this machine from the live
trades and quotes it happened to receive. Now the bars Alpaca publishes are downloaded instead, for every
monitored instrument at once (one request covers a hundred symbols), and the rebuilt bar is only a fallback
for minutes in which Alpaca cannot be reached.

Monitored instruments: those with a live subscription or a deployed strategy (their bars go through the trading
engine, exactly as the rebuilt ones did) and those with a stored intraday history (kept current for research
and validation, written straight to the bar store).

Restart safety: every request starts from the newest bar already stored for that series, so minutes missed
while the application was stopped are fetched on the first refresh after it starts (up to seven days; an older
gap is left to the history download). History is downloaded backwards from the present, so an interrupted
download always leaves one contiguous block and is simply continued.
"""
from __future__ import annotations

import threading
from datetime import datetime

import pandas as pd

from ..core.logging import get_logger
from ..database.models import AppSetting
from ..domain.models import AssetClass
from ..instruments.registry import market_of
from ..monitoring import audit
from .alpaca_bars import SOURCES, AlpacaBars, AlpacaBarsError, FeedNotAllowed, describe_source, in_session
from .calendars import get_calendar
from .quality import DataKind, validate_bars
from .sources import Dataset
from .timeframes import tf_delta

log = get_logger(__name__)
INTRADAY = ("1m", "5m", "15m", "30m", "1h")
RECENT = pd.Timedelta(hours=2)        # series this close to the present are refreshed together in one request
CATCH_UP = pd.Timedelta(days=7)       # how far back a refresh reaches after an interruption
FIRST_WINDOW = {"1m": pd.Timedelta(days=1), "5m": pd.Timedelta(days=5)}  # a series with nothing stored yet
OFFICIAL_FOR = pd.Timedelta(seconds=150)  # after a successful refresh, rebuilt bars are not stored for this long
HISTORY_KEY = "intraday:history"


def group_of(inst) -> str | None:
    """Which Alpaca data set covers an instrument: "stocks", "crypto" or None."""
    if inst.asset_class is AssetClass.CRYPTO and inst.quote_currency == "USD":
        return "crypto"
    if inst.asset_class in (AssetClass.STOCK, AssetClass.ETF) and market_of(inst) == "US":
        return "stocks"
    return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class IntradayData:
    def __init__(self, runtime) -> None:
        self.rt = runtime
        self.client_factory = None  # test hook: (group, key, secret, min_interval) -> AlpacaBars
        self._lock = threading.Lock()
        self._clients: dict[tuple, AlpacaBars] = {}
        self.received: dict[tuple[str, str], datetime] = {}   # (instrument, timeframe) -> when a new official bar last arrived
        self.covered: dict[str, datetime] = {}                # instrument -> last successful refresh that included it
        self.groups: dict[str, dict] = {}
        self.job: dict | None = None
        self._job_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._pending: set[tuple[str, str]] = set()           # series whose history is being downloaded right now
        self._published: dict[tuple[str, str], pd.Timestamp | None] = {}  # newest official bar of series that also hold rebuilt ones

    # ------------------------------------------------------------------ plumbing
    def _client(self, group: str, min_interval: float = 0.35) -> AlpacaBars | None:
        try:
            found = self.rt._provider_credentials("alpaca")
        except Exception:  # e.g. the vault key is missing: treated as "no key", never logged with its content
            found = None
        key, secret = (found[0].get("api_key", ""), found[0].get("api_secret", "")) if found else ("", "")
        if group == "stocks" and not (key and secret):
            return None
        tag = (group, hash((key, secret)), min_interval)
        if tag not in self._clients:
            make = self.client_factory or (lambda g, k, s, i: AlpacaBars(k, s, self.rt.settings.alpaca_base_url, min_interval=i, clock=self.rt.clock))
            # Without a key Alpaca allows only a few requests a minute.
            self._clients[tag] = make(group, key, secret, min_interval if key else max(min_interval, 6.0))
        return self._clients[tag]

    def official(self, instrument_id: str) -> bool:
        """Whether official bars are arriving for this instrument, so a bar rebuilt from ticks must not be stored as well."""
        last = self.covered.get(instrument_id)
        return last is not None and pd.Timestamp(self.rt.clock()) - pd.Timestamp(last) <= OFFICIAL_FOR

    def incomplete(self) -> set[tuple[str, str]]:
        return set(self._pending)

    def _coverage(self) -> dict[tuple[str, str], dict]:
        with self.rt.db.session() as session:
            return {(c["instrument_id"], c["timeframe"]): c for c in self.rt.store.coverage(session)}

    def _engine_ids(self) -> set[str]:
        return set(self.rt.live.subscribed) | {d.instrument_id for d in self.rt.deployments.values()}

    def _instrument(self, instrument_id: str):
        try:
            return self.rt.registry.get(instrument_id)
        except KeyError:
            return None

    # ------------------------------------------------------------------ the refresh, once a minute
    def tick(self) -> dict:
        if not self.rt.user_settings.autopilot.minute_update:
            return {"skipped": "switched off"}
        if not self._lock.acquire(blocking=False):
            return {"skipped": "the previous refresh is still running"}
        try:
            now = pd.Timestamp(self.rt.clock())
            engine = self._engine_ids()
            coverage = {k: c for k, c in self._coverage().items() if k[1] in INTRADAY}
            wanted: dict[str, dict[str, list]] = {"stocks": {}, "crypto": {}}
            for iid in sorted(engine | {k[0] for k in coverage}):
                inst = self._instrument(iid)
                group = group_of(inst) if inst is not None else None
                if group is None or self.rt.kinds.get(iid) == DataKind.SIMULATED.value:
                    continue
                exec_tf = self.rt._exec_tf(iid)
                for tf in INTRADAY:
                    stored = coverage.get((iid, tf))
                    if stored is None and not (iid in engine and tf in ("1m", exec_tf)):
                        continue
                    if (stored is not None and stored["kind"] == DataKind.SIMULATED.value) or ((iid, tf) in self._pending and iid not in engine):
                        continue
                    since = pd.Timestamp(stored["last"]) + tf_delta(tf) if stored is not None else None
                    if stored is not None and "live:" in stored["source"]:
                        since = self._after_rebuilt(iid, tf, SOURCES["crypto" if group == "crypto" else "iex"], since, now)
                    wanted[group].setdefault(tf, []).append((inst, since))
            return {group: self._refresh(group, by_tf, engine, now) for group, by_tf in wanted.items() if by_tf}
        finally:
            self._lock.release()

    def _after_rebuilt(self, instrument_id: str, timeframe: str, source: str, since: pd.Timestamp, now: pd.Timestamp) -> pd.Timestamp | None:
        """Where to resume a series that holds bars rebuilt from ticks: after its newest PUBLISHED bar, so that the rebuilt
        minutes of the last days are requested again and replaced. The answer is remembered; it is looked up once per start."""
        key = (instrument_id, timeframe)
        if key not in self._published:
            with self.rt.db.session() as session:
                self._published[key] = self.rt.store.last_with_source(session, instrument_id, timeframe, source)
        last = self._published[key]
        if last is None:
            return max(now - FIRST_WINDOW.get(timeframe, pd.Timedelta(days=15)), now - CATCH_UP)
        return min(since, last + tf_delta(timeframe))

    def _refresh(self, group: str, by_tf: dict[str, list], engine: set[str], now: pd.Timestamp) -> dict:
        status = self.groups.setdefault(group, {"state": "starting", "last_success": None, "last_error": "", "last_error_at": None, "bars": 0,
                                                "requests": 0, "rate_limited": 0, "after_close_done": False})
        feed = "crypto" if group == "crypto" else "iex"
        status.update(feed=SOURCES[feed], instruments=len({inst.id for targets in by_tf.values() for inst, _ in targets}))
        client = self._client(group)
        if client is None:
            status.update(state="no key", last_error="No Alpaca key is stored: stock bars cannot be downloaded")
            return {"skipped": "no Alpaca key"}
        open_now = True
        if group == "stocks":
            calendar = get_calendar("XNYS")
            open_now = calendar.is_open(now.to_pydatetime())
            if not open_now and not calendar.is_open((now - pd.Timedelta(minutes=20)).to_pydatetime()) and status["after_close_done"]:
                status["state"] = "market closed"
                return {"skipped": "market closed"}
        before = (client.requests, client.rate_limited)
        stored, direct, failures = 0, [], []
        try:
            for tf, targets in by_tf.items():
                delta = tf_delta(tf)
                due = []
                for inst, since in targets:
                    start = since if since is not None else now.floor(delta) - FIRST_WINDOW.get(tf, pd.Timedelta(days=15))
                    start = max(start, now - CATCH_UP)  # an older gap belongs to the history download
                    if start + delta <= now:  # otherwise no completed bar can exist yet
                        due.append((inst, start))
                recent = [d for d in due if d[1] >= now - RECENT]
                behind = [d for d in due if d[1] < now - RECENT]
                for batch, size in ((recent, 100), (behind, 25)):
                    for chunk in _chunks(batch, size):
                        starts = {inst.symbol: (inst, start) for inst, start in chunk}
                        for page in client.pages(list(starts), tf, min(s for _, s in starts.values()), None, feed):
                            for symbol, frame in page.items():
                                if symbol not in starts:
                                    continue
                                inst, start = starts[symbol]
                                clean = self._clean(inst, tf, frame[frame.index >= start], group)
                                if clean.empty:
                                    continue
                                self.received[(inst.id, tf)] = now.to_pydatetime()
                                self._published[(inst.id, tf)] = clean.index[-1]
                                if inst.id in engine and tf in ("1m", self.rt._exec_tf(inst.id)):
                                    try:
                                        stored += self.rt.ingest_official(inst.id, tf, clean, SOURCES[feed])
                                    except Exception as exc:  # e.g. no exchange rate yet: this instrument waits, the others carry on
                                        failures.append(f"{inst.id} {tf}: {type(exc).__name__}: {str(exc)[:120]}")
                                else:
                                    direct.append((inst.id, tf, clean))
            stored += self._write(direct, DataKind.REALTIME, SOURCES[feed], append=True)
        except AlpacaBarsError as exc:
            stored += self._write(direct, DataKind.REALTIME, SOURCES[feed], append=True)  # what did arrive is kept
            status.update(state="error", last_error=str(exc), last_error_at=now.isoformat(), bars=status["bars"] + stored,
                          requests=status["requests"] + client.requests - before[0], rate_limited=status["rate_limited"] + client.rate_limited - before[1])
            log.warning("intraday_refresh_failed", group=group, error=str(exc))
            return {"error": str(exc), "bars": stored}
        stamp = now.to_pydatetime()
        for targets in by_tf.values():
            for inst, _ in targets:
                self.covered[inst.id] = stamp
        status.update(state="ok" if open_now else "market closed", last_success=now.isoformat(), bars=status["bars"] + stored, after_close_done=not open_now,
                      requests=status["requests"] + client.requests - before[0], rate_limited=status["rate_limited"] + client.rate_limited - before[1],
                      last_error="; ".join(failures[:3]), last_error_at=now.isoformat() if failures else status["last_error_at"])
        return {"bars": stored, "requests": client.requests - before[0], "failed": len(failures)}

    def _clean(self, inst, timeframe: str, frame: pd.DataFrame, group: str) -> pd.DataFrame:
        """Regular-session bars only for stocks (the thin extended hours are left out), then the platform's usual validation."""
        if frame.empty:
            return frame
        if group == "stocks":
            frame = frame[in_session(frame.index, get_calendar(inst.calendar)).to_numpy()]
            if frame.empty:
                return frame
        return validate_bars(frame, timeframe, None)[0]

    def _write(self, items: list[tuple[str, str, pd.DataFrame]], kind: DataKind, source: str, append: bool = False) -> int:
        """Store bars without passing them to the trading engine (instruments nothing is trading or watching live)."""
        if not items:
            return 0
        total = 0
        with self.rt.db.session() as session:
            for iid, tf, frame in items:
                dataset = Dataset(frame, kind, source)
                total += self.rt.store.append(session, iid, tf, dataset) if append else self.rt.store.save(session, iid, tf, dataset)
        items.clear()
        return total

    # ------------------------------------------------------------------ history
    def backfill(self, instrument_ids: list[str], timeframes: list[str], months: int, actor: str, feed: str = "iex") -> dict:
        """Download `months` of intraday history for the given instruments, in the background. Continues what an earlier,
        interrupted download left and skips what is already there."""
        if self._job_thread is not None and self._job_thread.is_alive():
            raise ValueError("An intraday history download is already running")
        if feed not in ("iex", "sip"):
            raise ValueError("feed must be iex or sip")
        bad = [t for t in timeframes if t not in INTRADAY]
        if bad or not timeframes:
            raise ValueError(f"Intraday timeframes are {', '.join(INTRADAY)}")
        plan, skipped = [], []
        for iid in dict.fromkeys(instrument_ids):
            inst = self._instrument(iid)
            if inst is None or group_of(inst) is None or self.rt.kinds.get(iid) == DataKind.SIMULATED.value:
                skipped.append(iid)
                continue
            plan += [(inst, tf) for tf in timeframes]
        if not plan:
            raise ValueError("None of these instruments is covered by Alpaca's intraday data")
        self._cancel.clear()
        self._pending = {(inst.id, tf) for inst, tf in plan}
        self.job = {"status": "running", "started": self.rt.clock().isoformat(), "finished": None, "series": len(plan), "done": 0, "bars": 0, "requests": 0,
                    "rate_limited": 0, "failed": [], "skipped": skipped, "feed": feed, "months": months, "timeframes": list(timeframes), "current": None, "notes": []}
        with self.rt.db.session() as session:
            audit.record(session, actor, "intraday.backfill", instruments=len({i.id for i, _ in plan}), timeframes=list(timeframes), months=months, feed=feed)
        self._job_thread = threading.Thread(target=self._run_backfill, args=(plan, months, feed), name="intraday-history", daemon=True)
        self._job_thread.start()
        return self.status()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._job_thread is not None:
            self._job_thread.join(timeout)

    def _history(self) -> dict:
        with self.rt.db.session() as session:
            row = session.get(AppSetting, HISTORY_KEY)
            return dict(row.value) if row else {}

    def _remember(self, key: str, entry: dict) -> None:
        with self.rt.db.session() as session:
            row = session.get(AppSetting, HISTORY_KEY)
            if row is None:
                session.add(AppSetting(key=HISTORY_KEY, value={key: entry}))
            else:
                row.value = {**row.value, key: entry}

    def _run_backfill(self, plan: list, months: int, feed: str) -> None:
        job = self.job
        clients: dict[str, AlpacaBars | None] = {}
        try:
            for inst, tf in plan:
                if self._cancel.is_set():
                    job["status"] = "cancelled"
                    break
                job["current"] = f"{inst.id} {tf}"
                group = group_of(inst)
                if group not in clients:
                    clients[group] = self._client(group, min_interval=0.5)  # its own pace, leaving room for the minute refresh
                client = clients[group]
                try:
                    if client is None:
                        raise AlpacaBarsError("no Alpaca key is stored")
                    before = (client.requests, client.rate_limited)
                    try:
                        job["bars"] += self._download(inst, tf, months, "crypto" if group == "crypto" else job["feed"], client)
                    finally:
                        job["requests"] += client.requests - before[0]
                        job["rate_limited"] += client.rate_limited - before[1]
                except FeedNotAllowed as exc:
                    if job["feed"] == "sip" and group == "stocks":
                        # This plan cannot read the consolidated tape: carry on with IEX and say so.
                        job["feed"] = "iex"
                        job["notes"].append(f"Consolidated (SIP) history refused by Alpaca ({exc}); IEX history is downloaded instead")
                        try:
                            job["bars"] += self._download(inst, tf, months, "iex", client)
                        except AlpacaBarsError as again:
                            job["failed"].append({"series": f"{inst.id} {tf}", "reason": str(again)})
                    else:
                        job["failed"].append({"series": f"{inst.id} {tf}", "reason": str(exc)})
                except AlpacaBarsError as exc:
                    job["failed"].append({"series": f"{inst.id} {tf}", "reason": str(exc)})
                except Exception as exc:  # recorded; the remaining series carry on
                    job["failed"].append({"series": f"{inst.id} {tf}", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    log.error("intraday_history_failed", series=f"{inst.id} {tf}", error=type(exc).__name__)
                finally:
                    self._pending.discard((inst.id, tf))
                    job["done"] += 1
            else:
                job["status"] = "finished"
        finally:
            self._pending.clear()
            job.update(current=None, finished=self.rt.clock().isoformat())
            if job["status"] == "running":
                job["status"] = "failed"

    def _download(self, inst, tf: str, months: int, feed: str, client: AlpacaBars) -> int:
        """One series. Returns the bars stored. Raises AlpacaBarsError when Alpaca cannot serve it."""
        now, delta = pd.Timestamp(self.rt.clock()), tf_delta(tf)
        target = (now - pd.Timedelta(days=30.44 * months)).floor("D")
        key, source = f"{inst.id}|{tf}", SOURCES[feed]
        kind = DataKind.DELAYED if feed == "sip" else DataKind.HISTORICAL
        group = group_of(inst)
        with self.rt.db.session() as session:
            stored = {c["timeframe"]: c for c in self.rt.store.coverage_for(session, inst.id)}.get(tf)
            if stored is not None and stored["kind"] == DataKind.SIMULATED.value:
                raise AlpacaBarsError("holds simulated data; remove it first")
            span = self.rt.store.span(session, inst.id, tf) if stored is not None else None
            newest_of_feed = self.rt.store.last_with_source(session, inst.id, tf, source) if (stored is not None and feed == "sip") else None
        # (start, end, newest first). Backwards from the present for anything older than what is stored, so that an
        # interruption leaves one contiguous block; forwards from the newest stored bar for the rest.
        segments: list[tuple[pd.Timestamp, pd.Timestamp, bool]] = []
        if span is None:
            segments.append((target, now, True))
        else:
            first, last = span
            reached = self._history().get(key, {}).get("from")
            if first > target + pd.Timedelta(days=3) and not (reached and pd.Timestamp(reached) <= target):
                segments.append((target, first - delta, True))
            if feed == "sip":
                # Bars taken from IEX in the meantime are replaced by the consolidated ones as soon as those may be read.
                segments.append(((newest_of_feed + delta) if newest_of_feed is not None else first, now, False))
            elif inst.id not in self._engine_ids():
                segments.append((last + delta, now, False))  # followed instruments are kept current by the minute refresh
        total = 0
        for start, end, newest_first in segments:
            for page in client.pages([inst.symbol], tf, start, end, feed, newest_first=newest_first):
                if self._cancel.is_set():
                    return total
                clean = self._clean(inst, tf, page.get(inst.symbol, pd.DataFrame()), group)
                if len(clean):
                    total += self._write([(inst.id, tf, clean)], kind, source)
        self._remember(key, {"from": target.isoformat(), "feed": source, "done_at": now.isoformat(), "months": months})
        return total

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict:
        job = dict(self.job) if self.job else None
        if job:
            job["failed"] = job["failed"][:50]
        return {"minute_update": self.rt.user_settings.autopilot.minute_update, "groups": {g: {k: v for k, v in s.items() if k != "after_close_done"}
                                                                                         for g, s in self.groups.items()},
                "history_job": job, "history": self._history()}

    def freshness(self) -> dict:
        """For every monitored instrument: when data last arrived, from which feed, and how old the newest bar of each timeframe is."""
        now = pd.Timestamp(self.rt.clock())
        coverage = self._coverage()
        engine = self._engine_ids()
        ids = sorted(engine | {iid for (iid, tf) in coverage if tf in INTRADAY})
        market_open = get_calendar("XNYS").is_open(now.to_pydatetime())
        rows = []
        for iid in ids:
            inst = self._instrument(iid)
            if inst is None:
                continue
            group = group_of(inst)
            series = []
            for tf in (*INTRADAY, "4h", "1d", "1w"):
                c = coverage.get((iid, tf))
                if c is None:
                    continue
                closes = pd.Timestamp(c["last"]) + tf_delta(tf)
                series.append({"timeframe": tf, "bars": c["bars"], "last_bar_close": closes.isoformat(), "age_seconds": round((now - closes).total_seconds(), 1),
                               "source": c["source"], "feed": describe_source(c["source"]), "kind": c["kind"]})
            tick = self.rt.live_hub.latest.get(iid)
            arrivals = [pd.Timestamp(t) for (i, _), t in self.received.items() if i == iid]
            if tick is not None:
                arrivals.append(pd.Timestamp(tick.ts_received))
            last_update = max(arrivals) if arrivals else None
            newest = max((pd.Timestamp(s["last_bar_close"]) for s in series if s["timeframe"] in INTRADAY), default=None)
            reference = last_update or newest
            age = round((now - reference).total_seconds(), 1) if reference is not None else None
            closed = group == "stocks" and not market_open
            if age is None:
                state = "no data"
            elif closed:
                state = "market closed"
            else:
                state = "current" if age <= 180 else "late" if age <= 900 else "stale"
            rows.append({"instrument_id": iid, "symbol": inst.symbol, "group": group, "followed_live": iid in self.rt.live.subscribed,
                         "official_bars": self.official(iid), "last_update": reference.isoformat() if reference is not None else None,
                         "last_update_kind": "received" if last_update is not None else "newest stored bar", "age_seconds": age, "state": state,
                         "feed": describe_source(SOURCES["crypto" if group == "crypto" else "iex"]) if group else "", "series": series})
        return {"as_of": now.isoformat(), "us_market_open": market_open, "groups": self.status()["groups"], "instruments": rows}
