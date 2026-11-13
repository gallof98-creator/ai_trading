"""Global security catalog: synchronisation, de-duplication, history and coverage reporting."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..database.models import BarSeries, Security, SecurityEvent, SyncRun
from .discovery import EQUITY_TYPES, SECURITY_TYPES, ListingRecord, OpenFigi
from .exchanges import EXCHANGES, Exchange

# A provider answer much smaller than its previous one is treated as a truncated response,
# not as hundreds of simultaneous delistings.
DELIST_SAFETY_RATIO = 0.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(session: Session, security: Security, kind: str, old, new, source: str, ts: datetime) -> None:
    session.add(SecurityEvent(security_id=security.id, ts=ts, kind=kind, old=None if old is None else str(old)[:200],
                              new=None if new is None else str(new)[:200], source=source))


def sync_exchange(session: Session, provider: str, exchange: Exchange, records: list[ListingRecord], now: datetime | None = None) -> dict:
    """Merge one provider's listing of one exchange into the catalog.

    Matching order: FIGI, then ISIN (on this exchange), then ticker. A ticker match is rejected when
    both sides carry different ISINs - that is a reused ticker, i.e. a different security.
    Securities this provider used to list and no longer does are marked missing; they become
    'delisted' only when no other provider still lists them.
    """
    now = now or _now()
    counts = {"received": len(records), "new": 0, "updated": 0, "unchanged": 0, "ticker_changes": 0, "relisted": 0, "delisted": 0,
              "duplicates_in_feed": 0, "delist_check": "done"}
    existing = list(session.scalars(select(Security).where(Security.exchange == exchange.code)))
    by_figi = {s.figi: s for s in existing if s.figi}
    by_isin = {s.isin: s for s in existing if s.isin}
    by_ticker: dict[str, Security] = {}
    for s in sorted(existing, key=lambda s: s.listing_status != "active"):  # active listings win a ticker clash
        by_ticker.setdefault(s.ticker, s)
    previously_listed = [s for s in existing if provider in (s.sources or {}) and not s.sources[provider].get("missing_since")]
    seen: set[int] = set()
    seen_keys: set[str] = set()

    for rec in records:
        key = rec.figi or (f"isin:{rec.isin}" if rec.isin else f"ticker:{rec.ticker}")
        if key in seen_keys:
            counts["duplicates_in_feed"] += 1
            continue
        seen_keys.add(key)
        match = (by_figi.get(rec.figi) if rec.figi else None) or (by_isin.get(rec.isin) if rec.isin else None)
        if match is None:
            candidate = by_ticker.get(rec.ticker)
            reused = candidate is not None and ((candidate.isin and rec.isin and candidate.isin != rec.isin) or
                                                (candidate.figi and rec.figi and candidate.figi != rec.figi))
            match = None if reused else candidate
        source = {"symbol": rec.provider_symbol, "synced_at": now.isoformat()}
        if match is None:
            sec = Security(figi=rec.figi, share_class_figi=rec.share_class_figi, isin=rec.isin, ticker=rec.ticker, name=rec.name[:200],
                           exchange=exchange.code, mic=rec.mic, country=exchange.country, currency=rec.currency, security_type=rec.security_type,
                           raw_type=rec.raw_type[:64], listing_status="active", sources={provider: source}, first_seen=now, last_seen=now, last_synced=now)
            session.add(sec)
            session.flush()
            _event(session, sec, "listed", None, rec.ticker, provider, now)
            counts["new"] += 1
            if rec.figi:
                by_figi[rec.figi] = sec
            if rec.isin:
                by_isin[rec.isin] = sec
            by_ticker[rec.ticker] = sec
            seen.add(sec.id)
            continue

        changed = False
        if match.ticker != rec.ticker:
            _event(session, match, "ticker_changed", match.ticker, rec.ticker, provider, now)
            by_ticker.pop(match.ticker, None)
            match.ticker, changed = rec.ticker, True
            by_ticker[rec.ticker] = match
            counts["ticker_changes"] += 1
        if rec.name and match.name != rec.name[:200]:
            if match.name:
                _event(session, match, "name_changed", match.name, rec.name, provider, now)
            match.name, changed = rec.name[:200], True
        for attr in ("isin", "figi", "share_class_figi"):
            value = getattr(rec, attr)
            if value and not getattr(match, attr):
                setattr(match, attr, value)
                _event(session, match, "identifier_added", None, f"{attr}={value}", provider, now)
                changed = True
        # A provider with a specific type refines an unspecific one; it never overwrites one specific type with another silently.
        if rec.security_type != match.security_type and (match.security_type == "other" or rec.security_type != "other"):
            if match.security_type != "other" and rec.security_type != "other":
                _event(session, match, "type_changed", match.security_type, rec.security_type, provider, now)
            match.security_type, match.raw_type, changed = rec.security_type, rec.raw_type[:64], True
        if rec.mic and not match.mic:
            match.mic, changed = rec.mic, True
        if match.listing_status != "active":
            match.listing_status, changed = "active", True
            _event(session, match, "relisted", None, rec.ticker, provider, now)
            counts["relisted"] += 1
        match.sources = {**(match.sources or {}), provider: source}
        match.last_seen = match.last_synced = now
        seen.add(match.id)
        counts["updated" if changed else "unchanged"] += 1

    if previously_listed and len(records) < DELIST_SAFETY_RATIO * len(previously_listed):
        counts["delist_check"] = (f"skipped: the provider returned {len(records)} listings against {len(previously_listed)} last time, "
                                  "which looks like an incomplete answer")
    else:
        for s in previously_listed:
            if s.id in seen:
                continue
            sources = dict(s.sources)
            sources[provider] = {**sources[provider], "missing_since": now.isoformat()}
            s.sources, s.last_synced = sources, now
            if all(v.get("missing_since") for v in sources.values()) and s.listing_status == "active":
                s.listing_status = "delisted"
                _event(session, s, "delisted", s.ticker, None, provider, now)
                counts["delisted"] += 1
    session.add(SyncRun(provider=provider, exchange=exchange.code, started_at=now, finished_at=_now(), status="ok", counts=counts))
    return counts


def record_failed_sync(session: Session, provider: str, exchange: Exchange, message: str) -> None:
    now = _now()
    session.add(SyncRun(provider=provider, exchange=exchange.code, started_at=now, finished_at=now, status="failed", message=message[:1000]))


def enrich_with_figi(session: Session, figi: OpenFigi, limit: int) -> dict:
    """Add FIGI identifiers (and a more specific type where the catalog only knows 'other') for up to
    `limit` securities that lack one. Ambiguous lookups are left empty rather than guessed."""
    todo = list(session.scalars(select(Security).where(Security.figi.is_(None), Security.figi_checked_at.is_(None), Security.listing_status == "active")
                                .order_by(Security.id).limit(limit)))
    out = {"checked": 0, "matched": 0}
    now = _now()
    from .discovery import classify

    for i in range(0, len(todo), figi.batch_size):
        batch = todo[i:i + figi.batch_size]
        jobs = []
        for s in batch:
            exch = EXCHANGES[s.exchange].providers.get("openfigi") if s.exchange in EXCHANGES else None
            jobs.append({"isin": s.isin, "exch": exch} if s.isin else {"ticker": s.ticker.replace(".", "/") if s.exchange == "US" else s.ticker, "exch": exch})
        results = figi.lookup(jobs)
        for s, hit in zip(batch, results):
            s.figi_checked_at = now
            out["checked"] += 1
            if not hit or not hit.get("figi"):
                continue
            s.figi, s.composite_figi, s.share_class_figi = hit["figi"], hit.get("compositeFIGI"), hit.get("shareClassFIGI") or s.share_class_figi
            _event(session, s, "identifier_added", None, f"figi={hit['figi']}", "openfigi", now)
            # OpenFIGI's type refines the catalog's when the listing provider was unspecific
            # ("other", or a plain "Stock" label that does not separate REITs and ADRs from ordinary shares).
            unspecific = s.security_type == "other" or s.raw_type.startswith("Stock")
            if unspecific and hit.get("securityType"):
                refined = classify(hit["securityType"])
                if refined != "other" and refined != s.security_type:
                    if s.security_type != "other":
                        _event(session, s, "type_changed", s.security_type, refined, "openfigi", now)
                    s.security_type, s.raw_type = refined, str(hit["securityType"])[:64]
            if not s.name and hit.get("name"):
                s.name = str(hit["name"])[:200]
            out["matched"] += 1
        session.flush()
    return out


# ---------------------------------------------------------------- queries
def search(session: Session, q: str = "", exchanges: list[str] | None = None, countries: list[str] | None = None, types: list[str] | None = None,
           status: str | None = "active", has_prices: bool | None = None, limit: int = 100, offset: int = 0) -> dict:
    stmt = select(Security)
    text = q.strip()
    if text:
        upper = text.upper()
        stmt = stmt.where(or_(Security.ticker == upper, Security.ticker.like(f"{upper}%"), Security.isin == upper, Security.figi == upper,
                              func.lower(Security.name).like(f"%{text.lower()}%")))
    if exchanges:
        stmt = stmt.where(Security.exchange.in_(exchanges))
    if countries:
        stmt = stmt.where(Security.country.in_(countries))
    if types:
        stmt = stmt.where(Security.security_type.in_(types))
    if status:
        stmt = stmt.where(Security.listing_status == status)
    if has_prices is not None:
        priced = select(BarSeries.instrument_id)
        stmt = stmt.where(Security.instrument_id.in_(priced) if has_prices else or_(Security.instrument_id.is_(None), Security.instrument_id.not_in(priced)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    order = [Security.exchange, Security.ticker]
    if text:  # an exact ticker match comes first, then tickers starting with the text, then name matches
        upper = text.upper()
        order = [case((Security.ticker == upper, 0), (Security.ticker.like(f"{upper}%"), 1), else_=2), *order]
    rows = session.scalars(stmt.order_by(*order).limit(max(1, min(limit, 500))).offset(max(0, offset)))
    return {"total": total, "items": [to_dict(s) for s in rows]}


def to_dict(s: Security) -> dict:
    return {"id": s.id, "ticker": s.ticker, "name": s.name, "exchange": s.exchange, "mic": s.mic, "country": s.country, "currency": s.currency,
            "isin": s.isin, "figi": s.figi, "composite_figi": s.composite_figi, "share_class_figi": s.share_class_figi,
            "security_type": s.security_type, "raw_type": s.raw_type, "sector": s.sector, "industry": s.industry,
            "market_cap": float(s.market_cap) if s.market_cap is not None else None, "listing_status": s.listing_status,
            "trading_status": s.trading_status, "providers": sorted(s.sources or {}), "sources": s.sources, "instrument_id": s.instrument_id,
            "first_seen": s.first_seen.isoformat(), "last_seen": s.last_seen.isoformat(), "last_synced": s.last_synced.isoformat()}


def profile(session: Session, security_id: int) -> dict | None:
    s = session.get(Security, security_id)
    if s is None:
        return None
    events = session.scalars(select(SecurityEvent).where(SecurityEvent.security_id == security_id).order_by(SecurityEvent.id.desc()).limit(200))
    # Other listings of the same company: same share class (FIGI) or same ISIN on another exchange.
    same = []
    if s.share_class_figi or s.isin:
        cond = [c for c in ((Security.share_class_figi == s.share_class_figi) if s.share_class_figi else None,
                            (Security.isin == s.isin) if s.isin else None) if c is not None]
        same = [to_dict(o) for o in session.scalars(select(Security).where(or_(*cond), Security.id != s.id).limit(50))]
    return {**to_dict(s), "history": [{"ts": e.ts.isoformat(), "kind": e.kind, "old": e.old, "new": e.new, "source": e.source} for e in events],
            "other_listings": same}


def coverage_report(session: Session) -> dict:
    """What is actually in the catalog, per exchange and per provider. Built only from stored data."""
    rows = session.execute(select(Security.exchange, Security.listing_status, Security.security_type, func.count(), func.count(Security.isin),
                                  func.count(Security.figi), func.count(Security.instrument_id))
                           .group_by(Security.exchange, Security.listing_status, Security.security_type)).all()
    from ..instruments.registry import market_for_exchange
    from ..market_data.providers import PROVIDER_CLASSES

    priced = dict(session.execute(select(Security.exchange, func.count()).where(Security.instrument_id.in_(
        select(BarSeries.instrument_id).where(BarSeries.kind != "simulated", BarSeries.timeframe == "1d"))).group_by(Security.exchange)).all())
    # A price series is stale when its newest bar is more than a week old.
    cutoff_ms = int((_now().timestamp() - 7 * 86400) * 1000)
    stale = dict(session.execute(select(Security.exchange, func.count()).where(Security.instrument_id.in_(
        select(BarSeries.instrument_id).where(BarSeries.kind != "simulated", BarSeries.timeframe == "1d", BarSeries.last_ms < cutoff_ms)))
        .group_by(Security.exchange)).all())
    per: dict[str, dict] = {}
    for code, status, kind, n, n_isin, n_figi, n_linked in rows:
        e = per.setdefault(code, {"discovered": 0, "active": 0, "delisted": 0, "with_isin": 0, "with_figi": 0, "by_type": {t: 0 for t in SECURITY_TYPES},
                                  "equities_active": 0})
        e["discovered"] += n
        e["active" if status == "active" else "delisted"] += n
        e["with_isin"] += n_isin
        e["with_figi"] += n_figi
        if status == "active":
            e["by_type"][kind] = e["by_type"].get(kind, 0) + n
            if kind in EQUITY_TYPES:
                e["equities_active"] += n
    last: dict[tuple[str, str], SyncRun] = {}
    for run in session.scalars(select(SyncRun).order_by(SyncRun.id)):
        last[(run.exchange, run.provider)] = run
    exchanges = []
    for code, ex in EXCHANGES.items():
        stats = per.get(code, {"discovered": 0, "active": 0, "delisted": 0, "with_isin": 0, "with_figi": 0, "by_type": {}, "equities_active": 0})
        providers = {}
        for provider in ex.providers:
            if provider == "openfigi":
                continue
            run = last.get((code, provider))
            providers[provider] = ({"status": "never synchronised"} if run is None else
                                   {"status": run.status, "last_sync": run.finished_at.isoformat() if run.finished_at else None,
                                    "received": run.counts.get("received"), "message": run.message or run.counts.get("delist_check", "")})
        with_prices = priced.get(code, 0)
        market = market_for_exchange(code)
        price_providers = sorted(n for n, cls in PROVIDER_CLASSES.items() if market in cls.markets)
        exchanges.append({"code": code, "name": ex.name, "country": ex.country, "currency": ex.currency, "region": ex.region, "mic": ex.mic,
                          "price_providers": price_providers, "note": ex.note, **stats, "with_historical_prices": with_prices,
                          "stale_price_series": stale.get(code, 0),
                          # No provider connected here supplies live or delayed quotes for the catalog: stated, not left blank.
                          "with_realtime_data": 0, "with_delayed_data": 0, "missing_prices": max(stats["active"] - with_prices, 0), "providers": providers})
    totals = {k: sum(e[k] for e in exchanges) for k in ("discovered", "active", "delisted", "with_isin", "with_figi", "equities_active",
                                                         "with_historical_prices", "stale_price_series", "with_realtime_data", "with_delayed_data", "missing_prices")}
    synced = [e for e in exchanges if e["discovered"]]
    return {
        "generated_at": _now().isoformat(), "exchanges_in_registry": len(exchanges), "exchanges_with_data": len(synced), "totals": totals, "exchanges": exchanges,
        "statement": (f"The catalog holds {totals['discovered']} listings from {len(synced)} of {len(exchanges)} registered exchanges, as reported by the "
                      "connected providers. It is not a complete record of the world's listed securities: exchanges that were never synchronised, "
                      "securities a provider omits, and delistings before the first synchronisation are not in it."),
    }
