"""Celery tasks of the data platform (queue `ingest`), and the collection schedule.

The schedule only exists when QAT_DATA_SCHEDULE_ENABLED is true. It walks the configured watch lists and
skips, without failing, any source that is switched off or still waiting for its credentials.
"""
from __future__ import annotations

from functools import lru_cache

from ..core.config import get_settings
from ..core.logging import get_logger
from ..core.tasks import celery_app
from ..database.base import Database
from .http import SourceDisabled
from .service import DataHub
from .sources.crypto import VENUES

log = get_logger(__name__)


@lru_cache
def hub() -> DataHub:
    settings = get_settings()
    return DataHub(Database(settings.database_url), settings)


def _brief(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k != "results"} | {"failures": [{"key": r["key"], "status": r["status"], "message": r["message"]}
                                                                                for r in summary["results"] if r["status"] not in ("ok", "empty")][:20]}


@celery_app.task(name="data.ingest")
def ingest(provider: str, dataset: str, keys: list[str] | None = None, timeframe: str = "", start: str | None = None, end: str | None = None,
           options: dict | None = None) -> dict:
    return _brief(hub().ingest(provider, dataset, keys, timeframe, start, end, actor="schedule", **(options or {})))


def plan(group: str, settings=None) -> list[dict]:
    """The ingestions one schedule group performs, from the configured watch lists."""
    s = settings or get_settings()
    venues = sorted({p.split(":", 1)[0].strip().lower() for p in s.data_crypto_pairs.split(",") if ":" in p} & set(VENUES))
    equities = bool(s.data_equity_symbols.strip())
    if group == "fast":  # order books and trades: only exist going forward, so they are collected often
        return [{"provider": v, "dataset": d} for v in venues for d in ("orderbook", "trades")]
    if group == "bars":
        out = [{"provider": v, "dataset": "bars", "timeframe": s.data_crypto_timeframe} for v in venues]
        return out + ([{"provider": "alpaca", "dataset": "bars", "timeframe": s.data_equity_timeframe}] if equities else [])
    if group == "hourly":
        return [{"provider": "coingecko", "dataset": "crypto_venues"}]
    if group == "daily":
        out = [{"provider": "fred", "dataset": "macro"}, {"provider": "coingecko", "dataset": "crypto_market"}]
        return out + ([{"provider": "sec", "dataset": "fundamentals"}, {"provider": "fmp", "dataset": "company_profile"}] if equities else [])
    raise ValueError(f"Unknown schedule group {group!r}")


@celery_app.task(name="data.collect")
def collect(group: str) -> list[dict]:
    """Run one schedule group. Each ingestion stands alone: a source that is unavailable is reported and the rest carry on."""
    out = []
    for job in plan(group):
        try:
            out.append(_brief(hub().ingest(actor="schedule", **job)))
        except SourceDisabled as exc:
            out.append({**job, "skipped": str(exc)})
        except Exception as exc:  # the next run tries again
            log.error("data_collect_failed", **job, error=f"{type(exc).__name__}: {str(exc)[:300]}")
            out.append({**job, "error": type(exc).__name__})
    return out


def schedule(settings=None) -> dict:
    s = settings or get_settings()
    if not s.data_schedule_enabled:
        return {}
    from celery.schedules import crontab

    entries = {
        "data-bars": {"task": "data.collect", "schedule": 300.0, "args": ("bars",)},
        "data-hourly": {"task": "data.collect", "schedule": crontab(minute=7), "args": ("hourly",)},
        # After the US evening: daily Treasury yields and that day's filings are published by then.
        "data-daily": {"task": "data.collect", "schedule": crontab(hour=23, minute=30), "args": ("daily",)},
    }
    if s.data_orderbook_seconds > 0:
        entries["data-fast"] = {"task": "data.collect", "schedule": float(s.data_orderbook_seconds), "args": ("fast",)}
    return {name: {**entry, "options": {"queue": "ingest", "expires": 240}} for name, entry in entries.items()}


celery_app.conf.beat_schedule = schedule()
