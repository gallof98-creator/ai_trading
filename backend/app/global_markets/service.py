"""Global Markets application service: background catalog synchronisation, identifier enrichment,
and the link between catalog securities and the platform's tradable instruments."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select

from ..core.logging import get_logger
from ..database.models import Security
from ..instruments.registry import make_equity, market_for_exchange
from ..market_data.providers import ProviderAuthError, ProviderError, ProviderRateLimited
from ..monitoring import audit
from . import catalog
from .discovery import DISCOVERY_CLASSES, OpenFigi
from .exchanges import EXCHANGES, exchanges_for

log = get_logger(__name__)
# Catalog types that the trading and backtesting engines can hold as a stock-like instrument.
TRADABLE = {"common_stock": "stock", "reit": "stock", "adr": "stock", "etf": "etf"}


class CatalogJob:
    """Sequential background job: list each exchange from a provider, merge it, then enrich identifiers."""

    def __init__(self, service: "GlobalMarkets", provider_name: str, provider, exchanges: list[str], figi: OpenFigi | None, figi_limit: int,
                 pause: float, sleep: Callable[[float], None]) -> None:
        self.service, self.provider_name, self.provider, self.exchanges = service, provider_name, provider, exchanges
        self.figi, self.figi_limit, self.pause, self._sleep = figi, figi_limit, pause, sleep
        self._cancel, self._lock = threading.Event(), threading.Lock()
        self.state = {"status": "pending", "provider": provider_name, "total": len(exchanges), "done": 0, "current": None, "phase": "listing",
                      "results": [], "figi": {"checked": 0, "matched": 0}, "message": "", "started_at": None, "finished_at": None}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "results": list(self.state["results"]), "figi": dict(self.state["figi"])}

    def cancel(self) -> None:
        self._cancel.set()

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def run(self) -> None:
        db = self.service.runtime.db
        self._set(status="running", started_at=datetime.now(timezone.utc).isoformat())
        outcome, message = "finished", ""
        for i, code in enumerate(self.exchanges):
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelled by the user"
                break
            exchange = EXCHANGES[code]
            self._set(current=exchange.name)
            result: dict = {"exchange": code, "name": exchange.name}
            try:
                records = self.provider.list_securities(exchange)
                with db.session() as session:
                    result.update(status="ok", **catalog.sync_exchange(session, self.provider_name, exchange, records))
                    result["linked_to_instruments"] = self.service.link_instruments(session, code)
            except (ProviderAuthError, ProviderRateLimited) as exc:
                result.update(status="failed", message=str(exc))
                outcome, message = "failed", str(exc)
            except ProviderError as exc:
                result.update(status="failed", message=str(exc))
            except Exception as exc:  # database or unexpected problem: record it and continue with the next exchange
                log.error("catalog_sync_failed", exchange=code, error=str(exc))
                result.update(status="failed", message=f"{type(exc).__name__}: {str(exc)[:200]}")
            if result["status"] == "failed":
                try:
                    with db.session() as session:
                        catalog.record_failed_sync(session, self.provider_name, exchange, result["message"])
                except Exception:
                    pass
            with self._lock:
                self.state["results"].append(result)
                self.state["done"] += 1
            if outcome == "failed":
                break
            if i < len(self.exchanges) - 1:
                self._sleep(self.pause)
        if outcome == "finished" and self.figi is not None and self.figi_limit > 0:
            self._set(phase="identifiers", current="OpenFIGI")
            remaining = self.figi_limit
            try:
                while remaining > 0 and not self._cancel.is_set():
                    with db.session() as session:
                        step = catalog.enrich_with_figi(session, self.figi, min(self.figi.batch_size, remaining))
                    if step["checked"] == 0:
                        break
                    remaining -= step["checked"]
                    with self._lock:
                        self.state["figi"]["checked"] += step["checked"]
                        self.state["figi"]["matched"] += step["matched"]
                    self._sleep(self.figi.min_interval)
            except ProviderError as exc:
                message = f"Identifier lookup stopped: {exc}"
        self._set(status=outcome, message=message, current=None, finished_at=datetime.now(timezone.utc).isoformat())


class MetricsJob:
    """Recompute daily metrics for every instrument whose price series changed since its last computation,
    then run the saved scans that are triggered by a data update."""

    def __init__(self, service: "GlobalMarkets", series: list[dict], force: bool) -> None:
        self.service, self.series, self.force = service, series, force
        self._cancel, self._lock = threading.Event(), threading.Lock()
        self.completed = threading.Event()
        self.state = {"status": "pending", "total": len(series), "done": 0, "computed": 0, "skipped": 0, "failed": 0, "scans": [], "message": "",
                      "started_at": None, "finished_at": None}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "scans": list(self.state["scans"])}

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        import pandas as pd

        from ..database.models import InstrumentMetrics
        from .metrics import LOOKBACK_BARS, compute_metrics
        from .scanner import run_saved_scans

        rt = self.service.runtime
        with self._lock:
            self.state.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        outcome = "finished"
        try:
            for c in self.series:
                if self._cancel.is_set():
                    outcome = "cancelled"
                    break
                key = "failed"
                try:
                    as_of = c["last"][:10]
                    with rt.db.session() as session:
                        row = session.get(InstrumentMetrics, c["instrument_id"])
                        if row is not None and row.as_of == as_of and not self.force:
                            key = "skipped"
                        else:
                            start = pd.Timestamp(c["last"]) - pd.Timedelta(days=int(LOOKBACK_BARS * 1.6))
                            ds = rt.store.load(session, c["instrument_id"], "1d", start=start)
                            values = compute_metrics(ds.bars) if ds is not None else None
                            if values is None:
                                key = "skipped"  # too little history for any metric
                            else:
                                values["bars"] = c["bars"]
                                if row is None:
                                    session.add(InstrumentMetrics(instrument_id=c["instrument_id"], as_of=as_of, data_kind=c["kind"], values=values))
                                else:
                                    row.previous_regime = (row.values or {}).get("regime")
                                    row.as_of, row.data_kind, row.values, row.computed_at = as_of, c["kind"], values, datetime.now(timezone.utc)
                                key = "computed"
                except Exception as exc:
                    log.error("metrics_failed", instrument=c["instrument_id"], error=str(exc))
                with self._lock:
                    self.state[key] += 1
                    self.state["done"] += 1
            if outcome == "finished":
                with rt.db.session() as session:
                    scans = run_saved_scans(session, rt.registry, rt.notifier.notify)
                with self._lock:
                    self.state["scans"] = scans
        except Exception as exc:
            outcome = "failed"
            with self._lock:
                self.state["message"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            with self._lock:
                self.state.update(status=outcome, finished_at=datetime.now(timezone.utc).isoformat())
            self.completed.set()


class GlobalMarkets:
    KEYS = ("finnhub", "openfigi")  # keys this module adds to the runtime's provider key store

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.discovery_factory: Callable | None = None  # test hook: (name, key) -> DiscoveryProvider
        self.figi_factory: Callable | None = None  # test hook: (key) -> OpenFigi
        self.job: CatalogJob | None = None
        self.metrics_job: MetricsJob | None = None
        self.job_options: dict = {}
        self._lock = threading.Lock()

    # ---- providers
    def providers(self) -> list[dict]:
        out = []
        for name, cls in DISCOVERY_CLASSES.items():
            try:
                found = self.runtime._provider_key(name)
            except Exception:
                found = None
            out.append({"name": name, "label": cls.label, "configured": found is not None, "key_hint": f"…{found[0][-4:]}" if found else None,
                        "exchanges": [e.code for e in exchanges_for(name)], "verified": cls.verification})
        try:
            figi_key = self.runtime._provider_key("openfigi")
        except Exception:
            figi_key = None
        out.append({"name": "openfigi", "label": "OpenFIGI (identifiers)", "configured": True, "key_hint": f"…{figi_key[0][-4:]}" if figi_key else None,
                    "exchanges": [], "verified": "Works without a key; a free key makes identifier lookups about 40 times faster."})
        return out

    def _discovery(self, name: str):
        if name not in DISCOVERY_CLASSES:
            raise ValueError(f"Unknown discovery provider {name}")
        found = self.runtime._provider_key(name)
        if found is None:
            raise ValueError(f"No API key is configured for {DISCOVERY_CLASSES[name].label}")
        if self.discovery_factory is not None:
            return self.discovery_factory(name, found[0])
        return DISCOVERY_CLASSES[name](found[0], base_url=getattr(self.runtime.settings, f"{name}_base_url"))

    def _figi(self) -> OpenFigi:
        found = self.runtime._provider_key("openfigi")
        key = found[0] if found else ""
        return self.figi_factory(key) if self.figi_factory is not None else OpenFigi(key)

    # ---- synchronisation
    def start_sync(self, provider: str, actor: str, exchanges: list[str] | None = None, enrich_identifiers: bool = True, figi_limit: int = 3000) -> dict:
        with self._lock:
            if self.job is not None and self.job.snapshot()["status"] in ("pending", "running"):
                raise ValueError("A catalog synchronisation is already running")
            client = self._discovery(provider)
            supported = [e.code for e in exchanges_for(provider)]
            selected = supported if not exchanges else list(dict.fromkeys(exchanges))
            unknown = [c for c in selected if c not in EXCHANGES]
            if unknown:
                raise ValueError(f"Unknown exchange code(s): {', '.join(unknown)}")
            unsupported = [c for c in selected if c not in supported]
            if unsupported:
                raise ValueError(f"{DISCOVERY_CLASSES[provider].label} cannot list: {', '.join(unsupported)}")
            options = {"pause": 1.1, "sleep": time.sleep, **self.job_options}
            self.job = CatalogJob(self, provider, client, selected, self._figi() if enrich_identifiers else None, figi_limit, **options)
            with self.runtime.db.session() as session:
                audit.record(session, actor, "catalog.sync_started", provider=provider, exchanges=selected)
            threading.Thread(target=self.job.run, name="catalog-sync", daemon=True).start()
            return self.job.snapshot()

    def run_scheduled(self, every_hours: int, now: datetime | None = None) -> str | None:
        """Periodic synchronisation: starts one provider whose last successful run is older than `every_hours`.
        Returns the provider started, or None. Called from the application's background loop."""
        from ..database.models import SyncRun

        if every_hours <= 0 or (self.job is not None and self.job.snapshot()["status"] in ("pending", "running")):
            return None
        now = now or datetime.now(timezone.utc)
        for name in DISCOVERY_CLASSES:
            try:
                if self.runtime._provider_key(name) is None:
                    continue
            except Exception:
                continue
            with self.runtime.db.session() as session:
                last = session.scalar(select(SyncRun.finished_at).where(SyncRun.provider == name, SyncRun.status == "ok").order_by(SyncRun.id.desc()).limit(1))
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or (now - last).total_seconds() >= every_hours * 3600:
                self.start_sync(name, "system:schedule")
                return name
        return None

    def status(self) -> dict:
        return self.job.snapshot() if self.job is not None else {"status": "idle"}

    def cancel(self) -> None:
        if self.job is not None:
            self.job.cancel()

    # ---- metrics
    def start_metrics(self, actor: str, force: bool = False) -> dict:
        """Compute metrics for every daily series that changed (or all of them with force)."""
        with self._lock:
            if self.metrics_job is not None and not self.metrics_job.completed.is_set():
                raise ValueError("Metrics are already being computed")
            with self.runtime.db.session() as session:
                series = [c for c in self.runtime.store.coverage(session) if c["timeframe"] == "1d"]
                audit.record(session, actor, "scanner.metrics_started", series=len(series), force=force)
            self.metrics_job = MetricsJob(self, series, force)
            threading.Thread(target=self.metrics_job.run, name="metrics", daemon=True).start()
            return self.metrics_job.snapshot()

    def metrics_status(self) -> dict:
        return self.metrics_job.snapshot() if self.metrics_job is not None else {"status": "idle"}

    # ---- link to tradable instruments
    def _instrument_id(self, security: Security) -> str | None:
        market = market_for_exchange(security.exchange) if security.exchange in EXCHANGES else None
        kind = TRADABLE.get(security.security_type)
        if market is None or kind is None:
            return None
        from ..domain.models import AssetClass

        try:
            return make_equity(security.ticker, market, AssetClass(kind)).id
        except ValueError:
            return None

    def link_instruments(self, session, exchange_code: str) -> int:
        """Point catalog securities at instruments that already exist in the trading universe."""
        with self.runtime.lock:
            known = {i.id for i in self.runtime.registry.all()}
        linked = 0
        for s in session.scalars(select(Security).where(Security.exchange == exchange_code, Security.instrument_id.is_(None),
                                                         Security.listing_status == "active", Security.security_type.in_(list(TRADABLE)))):
            iid = self._instrument_id(s)
            if iid in known:
                s.instrument_id = iid
                linked += 1
        return linked

    def activate(self, security_ids: list[int], actor: str) -> dict:
        """Add catalog securities to the trading universe (so prices can be downloaded and strategies run)."""
        out: dict[str, list] = {"activated": [], "already_active": [], "unsupported": []}
        with self.runtime.db.session() as session:
            rows = list(session.scalars(select(Security).where(Security.id.in_(security_ids))))
            for s in rows:
                if s.instrument_id:
                    out["already_active"].append(s.id)
                    continue
                market = market_for_exchange(s.exchange) if s.exchange in EXCHANGES else None
                kind = TRADABLE.get(s.security_type)
                if s.listing_status != "active":
                    out["unsupported"].append({"id": s.id, "reason": "delisted"})
                elif market is None:
                    out["unsupported"].append({"id": s.id, "reason": "its exchange is not in the registry"})
                elif kind is None:
                    out["unsupported"].append({"id": s.id, "reason": f"{s.security_type.replace('_', ' ')} cannot be traded by the engines"})
                else:
                    result = self.runtime.add_instruments([s.ticker], market, kind, actor)
                    ids = result["created"] + result["existing"]
                    if ids:
                        s.instrument_id = ids[0]
                        out["activated"].append(s.id)
                    else:
                        out["unsupported"].append({"id": s.id, "reason": result["invalid"][0]["reason"] if result["invalid"] else "invalid ticker"})
            missing = set(security_ids) - {s.id for s in rows}
            out["unsupported"] += [{"id": i, "reason": "unknown security"} for i in sorted(missing)]
        return out
