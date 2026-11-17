"""The data platform's service: runs ingestions, keeps the catalog, reports provider health and data freshness.

One ingestion handles one (provider, dataset) for a list of keys (symbols, series, tickers). Keys are fetched
concurrently on one paced connection, validated, written to the lake and logged, each on its own: one
failing key never stops the others. Nothing here can reach a broker.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from ..core.config import Settings
from ..core.logging import get_logger
from ..database.base import Database
from ..database.models import AppSetting, BrokerAccount, DataIngestionRun, DataSeries
from ..market_data.timeframes import TIMEFRAMES, tf_delta
from . import quality
from .http import BudgetExhausted, SourceAuthError, SourceDisabled, SourceError, SourceNotFound, SourceRateLimited, scoped
from .lake import DataLake, now_utc
from .providers import PROVIDERS
from .sources.alpaca import AlpacaBars
from .sources.coingecko import CoinGeckoSource
from .sources.crypto import VENUES, CryptoVenue
from .sources.fmp import FmpSource
from .sources.fred import CADENCE_SECONDS, SERIES, FredSource
from .sources.sec import SecSource, cik10

log = get_logger(__name__)

SUPPORTED: dict[str, tuple[str, ...]] = {
    "fred": ("macro",), "sec": ("fundamentals", "filings"), "kraken": ("bars", "trades", "orderbook"), "binance": ("bars", "trades", "orderbook"),
    "coingecko": ("crypto_market", "crypto_venues"), "fmp": ("company_profile",), "alpaca": ("bars",),
}
DEFAULT_LOOKBACK = {"1m": 7, "5m": 30, "15m": 90, "30m": 180, "1h": 365, "4h": 730, "1d": 3650, "1w": 3650}  # days, when nothing is stored yet
MAX_KEYS = 200


class DataHubError(ValueError):
    pass


def _ts(value) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).as_unit("us")


class DataHub:
    def __init__(self, db: Database, settings: Settings, lake: DataLake | None = None, transports: dict | None = None,
                 exchange_factory: Callable[[str], object] | None = None, clock: Callable[[], pd.Timestamp] = now_utc) -> None:
        self.db, self.settings, self.clock = db, settings, clock
        self.lake = lake or DataLake(settings.data_lake_dir)
        self.transports = transports or {}  # test hook: provider name -> httpx transport
        self.exchange_factory = exchange_factory  # test hook: exchange id -> CCXT-like object
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ credentials and status
    def credentials(self, name: str) -> dict | None:
        """What a source needs to authenticate, or None. A value stored (encrypted) through the API wins over the environment."""
        from ..security.vault import CredentialVault

        vault = CredentialVault(self.settings.credential_key)
        with self.db.session() as session:
            row = session.get(AppSetting, f"provider:{name}")
            if row is not None:
                return vault.decrypt(row.value["ciphertext"])
            if name == "alpaca":  # the credentials of a linked Alpaca account also serve market data, as elsewhere in the platform
                from ..security.credentials import load_credentials

                for account in session.scalars(select(BrokerAccount).where(BrokerAccount.adapter == "alpaca").order_by(BrokerAccount.is_live)):
                    try:
                        creds = load_credentials(session, vault, account.id)
                    except LookupError:
                        continue
                    if creds.get("api_key") and creds.get("api_secret"):
                        return {"api_key": creds["api_key"], "api_secret": creds["api_secret"]}
        if name == "sec":
            return {"api_key": self.settings.sec_user_agent} if self.settings.sec_user_agent else None
        key = getattr(self.settings, f"{name}_api_key", "")
        return {"api_key": key, "api_secret": getattr(self.settings, f"{name}_api_secret", "")} if key else None

    def _ready(self, name: str) -> tuple[bool, str]:
        info = PROVIDERS[name]
        if not self.settings.data_source_enabled(name):
            return False, "switched off (QAT_DATA_SOURCES)"
        if not info.needs:
            return True, "no credentials needed"
        try:
            creds = self.credentials(name)
        except Exception as exc:  # e.g. the vault key is missing: say so instead of failing the whole status page
            return False, f"credentials cannot be read: {exc}"
        if creds is None or not creds.get("api_key") or (info.needs == "alpaca" and not creds.get("api_secret")):
            return False, {"api_key": "waiting for an API key", "user_agent": "waiting for QAT_SEC_USER_AGENT (name and contact e-mail)",
                           "alpaca": "waiting for Alpaca keys"}[info.needs]
        return True, "configured"

    def provider_status(self) -> list[dict]:
        health = self.health()
        out = []
        for name, info in PROVIDERS.items():
            ready, reason = self._ready(name)
            out.append({**info.to_dict(), "enabled": self.settings.data_source_enabled(name), "ready": ready, "state": reason,
                        "usage": self.usage(name), "health": health.get(name)})
        return out

    # ------------------------------------------------------------------ request budgets (free-plan allowances)
    def usage(self, name: str) -> dict:
        now = self.clock()
        with self.db.session() as session:
            row = session.get(AppSetting, f"provider_usage:{name}")
            usage = dict(row.value) if row else {}
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        return {"requests_today": usage.get("requests", 0) if usage.get("day") == day else 0,
                "requests_this_month": usage.get("month_requests", 0) if usage.get("month") == month else 0}

    def _add_usage(self, name: str, requests: int) -> None:
        if requests <= 0:
            return
        now = self.clock()
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        with self.db.session() as session:
            row = session.get(AppSetting, f"provider_usage:{name}")
            usage = dict(row.value) if row else {}
            if usage.get("month") != month:
                usage.update(month=month, month_requests=0, symbols=[])
            if usage.get("day") != day:
                usage.update(day=day, requests=0)
            usage["requests"] = usage.get("requests", 0) + requests
            usage["month_requests"] = usage.get("month_requests", 0) + requests
            if row is None:
                session.add(AppSetting(key=f"provider_usage:{name}", value=usage))
            else:
                row.value = usage

    def _budget(self, name: str, stats) -> Callable[[], None] | None:
        """A check run before every request: refuses once the provider's free allowance would be exceeded."""
        daily = {"fmp": self.settings.fmp_daily_budget}.get(name)
        monthly = {"coingecko": self.settings.coingecko_monthly_budget}.get(name)
        if not daily and not monthly:
            return None
        used = self.usage(name)

        def check() -> None:
            if daily and used["requests_today"] + stats().requests >= daily:
                raise BudgetExhausted(f"{PROVIDERS[name].label}: today's request budget ({daily}) is used up")
            if monthly and used["requests_this_month"] + stats().requests >= monthly:
                raise BudgetExhausted(f"{PROVIDERS[name].label}: this month's request budget ({monthly}) is used up")

        return check

    # ------------------------------------------------------------------ ingestion
    def ingest(self, provider: str, dataset: str, keys: list[str] | None = None, timeframe: str = "", start=None, end=None,
               actor: str = "system", **options) -> dict:
        """Fetch, validate and store. Blocks until done; returns a summary with one entry per key."""
        return asyncio.run(self._ingest(provider, dataset, keys, timeframe, start, end, actor, options))

    def start(self, provider: str, dataset: str, keys: list[str] | None = None, timeframe: str = "", start=None, end=None,
              actor: str = "system", **options) -> dict:
        """Run an ingestion in the background and return at once. Progress: `job(id)`; results also land in the ingestion log."""
        self._validate(provider, dataset, timeframe)
        job_id = uuid.uuid4().hex[:12]
        state = {"id": job_id, "status": "running", "provider": provider, "dataset": dataset, "keys": keys, "timeframe": timeframe,
                 "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "result": None, "error": None}
        with self._lock:
            self.jobs[job_id] = state
            for old in sorted(self.jobs, key=lambda k: self.jobs[k]["started_at"])[:-50]:
                del self.jobs[old]

        def run() -> None:
            try:
                state["result"] = self.ingest(provider, dataset, keys, timeframe, start, end, actor, **options)
                state["status"] = "finished"
            except Exception as exc:  # reported on the job; never raised into the server
                state.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                state["finished_at"] = datetime.now(timezone.utc).isoformat()

        thread = threading.Thread(target=run, name=f"data-ingest-{job_id}", daemon=True)
        state["_thread"] = thread
        thread.start()
        return self.job(job_id)

    def job(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self.jobs:
                raise LookupError(f"Unknown job {job_id}")
            return {k: v for k, v in self.jobs[job_id].items() if not k.startswith("_")}

    def _validate(self, provider: str, dataset: str, timeframe: str) -> None:
        if provider not in SUPPORTED:
            raise DataHubError(f"Unknown data source {provider!r}; available: {', '.join(SUPPORTED)}")
        if dataset not in SUPPORTED[provider]:
            raise DataHubError(f"{provider} does not provide {dataset!r}; it provides: {', '.join(SUPPORTED[provider])}")
        if dataset == "bars" and timeframe not in TIMEFRAMES:
            raise DataHubError(f"Unsupported timeframe {timeframe!r}; supported: {', '.join(TIMEFRAMES)}")
        ready, reason = self._ready(provider)
        if not ready:
            raise SourceDisabled(f"{PROVIDERS[provider].label} is not available: {reason}")

    async def _ingest(self, provider: str, dataset: str, keys: list[str] | None, timeframe: str, start, end, actor: str, options: dict) -> dict:
        self._validate(provider, dataset, timeframe)
        start, end = _ts(start), _ts(end)
        if start is not None and end is not None and end <= start:
            raise DataHubError("end must be after start")
        keys = [k.strip() for k in (keys or self._default_keys(provider, dataset)) if k and k.strip()]
        if not keys:
            raise DataHubError("Nothing to ingest: no keys given and no default list for this dataset")
        if len(keys) > MAX_KEYS:
            raise DataHubError(f"At most {MAX_KEYS} keys per ingestion")
        began = time.monotonic()
        context = _Run(self, provider, dataset, timeframe if dataset == "bars" else "", start, end, actor, options)
        results = await context.run(list(dict.fromkeys(keys)))
        self._add_usage(provider, context.requests)
        summary = {"provider": provider, "dataset": dataset, "timeframe": context.timeframe, "keys": len(results),
                   "ok": sum(r["status"] in ("ok", "empty") for r in results), "failed": sum(r["status"] not in ("ok", "empty") for r in results),
                   "rows_new": sum(r["rows_new"] for r in results), "rows_revised": sum(r["rows_revised"] for r in results),
                   "requests": context.requests, "seconds": round(time.monotonic() - began, 3), "results": results}
        log.info("data_ingested", **{k: v for k, v in summary.items() if k != "results"})
        return summary

    def _default_keys(self, provider: str, dataset: str) -> list[str]:
        s = self.settings
        split = lambda text: [p.strip() for p in text.split(",") if p.strip()]  # noqa: E731
        if provider == "fred":
            return split(s.data_macro_series) or list(SERIES)
        if provider in VENUES:
            return [p.split(":", 1)[1] for p in split(s.data_crypto_pairs) if p.split(":", 1)[0].lower() == provider and ":" in p]
        if provider == "coingecko":
            return split(s.data_coingecko_ids)
        return [t.upper() for t in split(s.data_equity_symbols)]

    # ------------------------------------------------------------------ catalog, freshness, health
    def _record(self, run: DataIngestionRun | None, *series: dict) -> None:
        """Append a log entry and bring the catalog rows of the series it touched up to date."""
        with self.db.session() as session:
            if run is not None:
                session.add(run)
            for entry in series:
                row = session.get(DataSeries, (entry["dataset"], entry["provider"], entry["key"], entry["timeframe"]))
                if row is None:
                    session.add(DataSeries(**entry))
                else:
                    for name, value in entry.items():
                        setattr(row, name, value)

    def catalog(self, dataset: str | None = None, provider: str | None = None) -> list[dict]:
        """Every stored series with its coverage and freshness."""
        now = self.clock()
        query = select(DataSeries).order_by(DataSeries.dataset, DataSeries.provider, DataSeries.key, DataSeries.timeframe)
        if dataset:
            query = query.where(DataSeries.dataset == dataset)
        if provider:
            query = query.where(DataSeries.provider == provider)
        out = []
        with self.db.session() as session:
            for r in session.scalars(query):
                last = _ts(r.last_ts)
                out.append({"dataset": r.dataset, "provider": r.provider, "key": r.key, "timeframe": r.timeframe, "rows": r.rows, "bytes": r.bytes,
                            "first": _ts(r.first_ts).isoformat() if r.first_ts else None, "last": last.isoformat() if last is not None else None,
                            "last_ingested_at": _ts(r.last_ingested_at).isoformat() if r.last_ingested_at else None, "last_status": r.last_status,
                            "cadence_seconds": r.cadence_seconds, **quality.freshness(last, r.cadence_seconds, now), "detail": r.detail})
        return out

    def runs(self, limit: int = 100, provider: str | None = None, dataset: str | None = None) -> list[dict]:
        query = select(DataIngestionRun).order_by(DataIngestionRun.id.desc()).limit(max(1, min(limit, 1000)))
        if provider:
            query = query.where(DataIngestionRun.provider == provider)
        if dataset:
            query = query.where(DataIngestionRun.dataset == dataset)
        with self.db.session() as session:
            return [{"id": r.id, "provider": r.provider, "dataset": r.dataset, "key": r.key, "timeframe": r.timeframe, "status": r.status,
                     "started_at": _ts(r.started_at).isoformat(), "finished_at": _ts(r.finished_at).isoformat() if r.finished_at else None,
                     "requests": r.requests, "rate_limited": r.rate_limited, "fetch_ms": r.fetch_ms, "write_ms": r.write_ms, "rows_in": r.rows_in,
                     "rows_new": r.rows_new, "rows_revised": r.rows_revised, "quality": r.quality, "message": r.message, "actor": r.actor}
                    for r in session.scalars(query)]

    def health(self, hours: int = 24) -> dict[str, dict]:
        """Per provider, from the ingestion log of the last `hours`: availability, rate limiting and speed."""
        since = (self.clock() - pd.Timedelta(hours=hours)).to_pydatetime()
        out: dict[str, dict] = {}
        with self.db.session() as session:
            rows = list(session.scalars(select(DataIngestionRun).where(DataIngestionRun.started_at >= since).order_by(DataIngestionRun.id)))
        for r in rows:
            h = out.setdefault(r.provider, {"runs": 0, "ok": 0, "failed": 0, "rate_limited": 0, "requests": 0, "fetch_ms": 0, "rows_new": 0,
                                            "last_success": None, "last_failure": None, "last_error": ""})
            h["runs"] += 1
            h["requests"] += r.requests
            h["rate_limited"] += r.rate_limited
            h["fetch_ms"] += r.fetch_ms
            h["rows_new"] += r.rows_new
            stamp = _ts(r.finished_at or r.started_at).isoformat()
            if r.status in ("ok", "empty"):
                h["ok"] += 1
                h["last_success"] = stamp
            else:
                h["failed"] += 1
                h["last_failure"], h["last_error"] = stamp, r.message[:200]
        for h in out.values():
            h["availability"] = round(h["ok"] / h["runs"], 4) if h["runs"] else None
            h["ms_per_request"] = round(h.pop("fetch_ms") / h["requests"], 1) if h["requests"] else None
            h["window_hours"] = hours
        return out

    # ------------------------------------------------------------------ small on-disk cache (reference lists)
    def cached_json(self, name: str, max_age: pd.Timedelta):
        path = Path(self.lake.root) / "_cache" / f"{name}.json"
        if not path.exists() or (time.time() - path.stat().st_mtime) > max_age.total_seconds():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def store_json(self, name: str, value) -> None:
        path = Path(self.lake.root) / "_cache" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)


class _Run:
    """One ingestion: owns the provider connection and writes one log entry per key."""

    def __init__(self, hub: DataHub, provider: str, dataset: str, timeframe: str, start, end, actor: str, options: dict) -> None:
        self.hub, self.provider, self.dataset, self.timeframe = hub, provider, dataset, timeframe
        self.start, self.end, self.actor, self.options = start, end, actor, options
        self.requests = 0
        self._stats = None

    def _stats_now(self):
        return self._stats

    def _client(self, source):
        client = source.client(transport=self.hub.transports.get(self.provider), budget=None)
        self._stats = client.stats
        client._budget = self.hub._budget(self.provider, self._stats_now)
        return client

    def _stored_last(self, dataset: str, key: str, timeframe: str = "") -> pd.Timestamp | None:
        with self.hub.db.session() as session:
            row = session.get(DataSeries, (dataset, self.provider, key, timeframe))
            return _ts(row.last_ts) if row is not None and row.last_ts is not None else None

    def _bar_start(self, key: str) -> pd.Timestamp:
        """Where to resume a bar series: just after the newest stored bar, or a default lookback when there is none."""
        if self.start is not None:
            return self.start
        last = self._stored_last("bars", key, self.timeframe)
        if last is not None:
            return last + tf_delta(self.timeframe)
        days = int(self.options.get("days") or DEFAULT_LOOKBACK[self.timeframe])
        return (self.hub.clock() - pd.Timedelta(days=days)).floor(tf_delta(self.timeframe) if tf_delta(self.timeframe) < pd.Timedelta(days=1) else "D")

    async def run(self, keys: list[str]) -> list[dict]:
        hub, s = self.hub, self.hub.settings
        creds = hub.credentials(self.provider) or {}
        venue = client = None
        try:
            if self.provider in VENUES:
                exchange = hub.exchange_factory(self.provider) if hub.exchange_factory else None
                venue = CryptoVenue(self.provider, exchange, binance_url=s.binance_data_url)
                self._stats = venue.stats
                fetch = self._crypto(venue)
            else:
                source = {"fred": lambda: FredSource(creds.get("api_key", ""), s.fred_base_url),
                          "sec": lambda: SecSource(creds.get("api_key", ""), s.sec_data_url, s.sec_www_url),
                          "coingecko": lambda: CoinGeckoSource(creds.get("api_key", ""), s.coingecko_base_url),
                          "fmp": lambda: FmpSource(creds.get("api_key", ""), s.fmp_base_url),
                          "alpaca": lambda: AlpacaBars(creds.get("api_key", ""), creds.get("api_secret", ""), s.alpaca_base_url)}[self.provider]()
                client = self._client(source)
                await client.__aenter__()
                fetch = getattr(self, f"_{self.provider}")(source, client)
                if self.provider == "sec":
                    self._tickers = await self._sec_tickers(source, client)
            gate = asyncio.Semaphore(s.data_concurrency)

            async def one(key: str) -> dict:
                async with gate:
                    return await self._one(key, fetch)

            return list(await asyncio.gather(*(one(k) for k in keys)))
        finally:
            self.requests = self._stats.requests if self._stats is not None else 0
            if venue is not None:
                await venue.close()
            if client is not None:
                await client.__aexit__(None, None, None)

    async def _one(self, key: str, fetch) -> dict:
        """Fetch, validate, store and log one key. Never raises: the outcome is in the returned entry and in the log."""
        hub = self.hub
        run = DataIngestionRun(provider=self.provider, dataset=self.dataset, key=key[:96], timeframe=self.timeframe, status="failed", actor=self.actor,
                               started_at=datetime.now(timezone.utc), requests=0, rate_limited=0, fetch_ms=0, write_ms=0, rows_in=0, rows_new=0,
                               rows_revised=0, quality={}, message="")
        mine = scoped()  # this key's own request count, even while other keys use the same connection
        series: list[dict] = []
        t0 = time.monotonic()
        try:
            writes, report, notes = await fetch(key)
            run.fetch_ms = int((time.monotonic() - t0) * 1000)
            t1 = time.monotonic()
            written = {"rows_in": 0, "rows_new": 0, "rows_revised": 0}
            for dataset, frame, parts, cadence, detail in writes:
                result = await asyncio.to_thread(hub.lake.write, dataset, frame)
                if dataset == self.dataset:
                    written = {"rows_in": result.rows_in, "rows_new": result.rows_new, "rows_revised": result.rows_revised}
                stats = hub.lake.stats(dataset, **parts) if len(frame) or dataset == self.dataset else None
                if stats is not None:
                    entry = {"dataset": dataset, "provider": self.provider, "key": key[:96], "timeframe": parts.get("timeframe", ""), "rows": stats["rows"],
                             "bytes": stats["bytes"], "first_ts": stats["first"].to_pydatetime() if stats["first"] is not None else None,
                             "last_ts": stats["last"].to_pydatetime() if stats["last"] is not None else None, "cadence_seconds": cadence,
                             "last_ingested_at": datetime.now(timezone.utc), "last_status": "ok", "detail": detail}
                    series.append(entry)
            run.write_ms = int((time.monotonic() - t1) * 1000)
            run.rows_in, run.rows_new, run.rows_revised = report.get("rows_in", written["rows_in"]), written["rows_new"], written["rows_revised"]
            run.quality = {**report, **({"notes": notes} if notes else {})}
            run.status = "ok" if written["rows_new"] or written["rows_revised"] or written["rows_in"] else "empty"
        except SourceDisabled as exc:
            run.status, run.message = "disabled", str(exc)
        except BudgetExhausted as exc:
            run.status, run.message = "limited", str(exc)
        except SourceRateLimited as exc:
            run.status, run.message = "limited", str(exc)
        except (SourceAuthError, SourceNotFound, SourceError, DataHubError, ValueError) as exc:
            run.message = str(exc)[:500]
        except Exception as exc:  # unexpected: recorded with its type, the other keys carry on
            run.message = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.error("data_ingest_failed", provider=self.provider, dataset=self.dataset, key=key, error=run.message)
        run.requests, run.rate_limited = mine.requests, mine.rate_limited
        run.fetch_ms = run.fetch_ms or int((time.monotonic() - t0) * 1000)
        run.finished_at = datetime.now(timezone.utc)
        entry = {"key": key, "status": run.status, "message": run.message, "rows_in": run.rows_in, "rows_new": run.rows_new, "rows_revised": run.rows_revised,
                 "requests": run.requests, "fetch_ms": run.fetch_ms, "write_ms": run.write_ms, "quality": run.quality or {}}
        await asyncio.to_thread(hub._record, run, *series)
        return entry

    # ---- per-provider fetchers: key -> ([(dataset, frame, series parts, cadence seconds, catalog detail)], quality report, notes)
    def _crypto(self, venue: CryptoVenue):
        depth = int(self.options.get("depth") or self.hub.settings.data_orderbook_depth)

        async def fetch(symbol: str):
            parts = {"source": self.provider, "symbol": symbol}
            if self.dataset == "bars":
                frame, notes = await venue.bars(symbol, self.timeframe, self._bar_start(symbol), self.end)
                clean, report = quality.check_bars(frame, self.timeframe, "24x7")
                return [("bars", clean, {**parts, "timeframe": self.timeframe}, int(tf_delta(self.timeframe).total_seconds()), {"venue": self.provider})], report, notes
            if self.dataset == "trades":
                since = self.start or self._stored_last("trades", symbol) or (self.hub.clock() - pd.Timedelta(hours=1))
                frame, notes = await venue.trades(symbol, since, self.end, int(self.options.get("max_rows") or 200_000))
                clean, report = quality.check_rows(frame, ["ts", "trade_id"], "price", "ts")
                return [("trades", clean, parts, self.hub.settings.data_orderbook_seconds or 60, {"venue": self.provider})], report, notes
            frame = await venue.order_book(symbol, depth)
            crossed = bool((frame["best_bid"] >= frame["best_ask"]).any())
            report = {"rows_in": 1, "rows_out": 0 if crossed else 1, "invalid_rows_removed": int(crossed), "ok": not crossed}
            return [("orderbook", frame[frame["best_bid"] < frame["best_ask"]], parts, self.hub.settings.data_orderbook_seconds or 60,
                     {"venue": self.provider, "depth": depth})], report, {}

        return fetch

    def _fred(self, source: FredSource, client):
        async def fetch(series_id: str):
            series_id = series_id.upper()
            start = self.start.date() if self.start is not None else None
            if start is None and series_id in SERIES and SERIES[series_id].pit == "lag":
                last = self._stored_last("macro", series_id)
                start = (last - pd.Timedelta(days=30)).date() if last is not None else None  # daily series: only the recent part is re-read
            frame, notes = await source.observations(client, series_id, start, self.options.get("pit"))
            clean, report = quality.check_rows(frame, ["series_id", "date", "realtime_start"], "value", "date")
            report["missing_values"] = notes.pop("missing_values", 0)
            info = SERIES.get(series_id)
            detail = {"title": info.title if info else "", "pit": notes.get("pit"), "release_time_new_york": info.release_time if info else ""}
            return [("macro", clean, {"source": "fred", "series_id": series_id}, CADENCE_SECONDS[info.cadence] if info else None, detail)], report, notes

        return fetch

    async def _sec_tickers(self, source: SecSource, client) -> dict:
        cached = self.hub.cached_json("sec_tickers", pd.Timedelta(days=7))
        if cached is None:
            cached = await source.tickers(client)
            self.hub.store_json("sec_tickers", cached)
        return cached

    def _sec(self, source: SecSource, client):
        async def fetch(key: str):
            key = key.upper()
            if key.isdigit():
                cik, ticker = cik10(key), next((t for t, v in self._tickers.items() if v["cik"] == cik10(key)), "")
            elif key in self._tickers:
                cik, ticker = self._tickers[key]["cik"], key
            else:
                raise SourceNotFound(f"SEC EDGAR lists no company with the ticker {key}")
            parts = {"source": "sec", "cik": cik}
            detail = {"ticker": ticker, "cik": cik, "name": self._tickers.get(ticker, {}).get("title", "")}
            filings = await source.filings(client, cik, ticker)
            filings, filing_report = quality.check_rows(filings, ["accession"], None, "ts")
            writes = [("filings", filings, parts, 86400 * 30, detail)]
            if self.dataset == "filings":
                return writes, filing_report, {}
            facts = await source.facts(client, cik, ticker, filings.set_index("accession")["ts"] if len(filings) else None)
            facts, report = quality.check_rows(facts, ["concept", "unit", "period_start", "period_end", "accession"], "value", "period_end")
            report["availability"] = facts["availability"].value_counts().to_dict() if len(facts) else {}
            return [*writes, ("fundamentals", facts, parts, 86400 * 92, detail)], report, {"filings": len(filings)}

        return fetch

    def _coingecko(self, source: CoinGeckoSource, client):
        async def fetch(coin_id: str):
            coin_id = coin_id.lower()
            parts = {"source": "coingecko", "coin_id": coin_id}
            if self.dataset == "crypto_market":
                days = int(self.options.get("days") or 365)
                frame = await source.market_chart(client, coin_id, days)
                clean, report = quality.check_rows(frame, ["ts"], "price", "ts")
                return [("crypto_market", clean, parts, 86400 if days > 90 else 3600, {"vs_currency": "usd"})], report, {}
            frame = await source.venues(client, coin_id)
            clean, report = quality.check_rows(frame, ["ts", "venue", "pair"], "last", None)
            return [("crypto_venues", clean, parts, 3600, {})], report, {}

        return fetch

    def _fmp(self, source: FmpSource, client):
        async def fetch(symbol: str):
            frame = await source.profile(client, symbol)
            clean, report = quality.check_rows(frame, ["ts", "symbol"], None, None)
            return [("company_profile", clean, {"source": "fmp"}, 86400, {})], report, {}

        return fetch

    def _alpaca(self, source: AlpacaBars, client):
        async def fetch(symbol: str):
            asset = "crypto" if "/" in symbol else "stocks"
            symbol = symbol.upper()
            frame, notes = await source.bars(client, symbol, self.timeframe, self._bar_start(symbol), self.end, asset)
            clean, report = quality.check_bars(frame, self.timeframe, "24x7" if asset == "crypto" else "XNYS")
            detail = {"venue": "SIP, 15 minutes delayed, adjusted" if asset == "stocks" else "Alpaca crypto", "asset": asset}
            return [("bars", clean, {"source": "alpaca", "timeframe": self.timeframe, "symbol": symbol}, int(tf_delta(self.timeframe).total_seconds()), detail)], report, notes

        return fetch

